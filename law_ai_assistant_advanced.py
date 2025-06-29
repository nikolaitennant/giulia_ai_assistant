# ─────────────────────────── law_ai_assistant.py (v1.6) ───────────────────────────
import os, io, re, base64, tempfile, shutil
from typing import List, Dict, Union
import psutil, shutil, humanize, os, time
import gc  

import streamlit as st
from dotenv import load_dotenv
from PIL import Image
import pytesseract
import faiss
import nltk, ssl
import zipfile
ssl._create_default_https_context = ssl._create_unverified_context  # avoids SSL issues on some hosts
nltk.download("punkt", quiet=True)
nltk.download("punkt_tab", quiet=True)

from openai import OpenAI

# ── LangChain (version-agnostic retriever import shim) ────────────────────────
def _import(cls):
    for p in ("langchain_community.retrievers", "langchain.retrievers"):
        try:
            return getattr(__import__(p, fromlist=[cls]), cls)
        except (ImportError, AttributeError):
            continue
    return None
BM25Retriever     = _import("BM25Retriever")
EnsembleRetriever = _import("EnsembleRetriever")

from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import (
    PyPDFLoader, UnstructuredPowerPointLoader, Docx2txtLoader,
    UnstructuredWordDocumentLoader, TextLoader, CSVLoader, UnstructuredImageLoader,
)
from langchain_core.documents import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.messages import SystemMessage, HumanMessage

try:
    from sentence_transformers import CrossEncoder
except ImportError:
    CrossEncoder = None

# Optional Word parser fallback
try: import docx2txt  # noqa: F401
except ImportError:
    Docx2txtLoader = TextLoader
    UnstructuredWordDocumentLoader = TextLoader

# ── Keys & constants ─────────────────────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY: st.error("OPENAI_API_KEY not set"); st.stop()

client      = OpenAI(api_key=OPENAI_API_KEY)
EMBED_MODEL = "text-embedding-3-small"
LLM_MODEL   = "gpt-4o-mini"

CTX_DIR   = "default_context"
INDEX_DIR = "faiss_store"
CHUNK_SZ  = 600
CHUNK_OV  = 100
FIRST_K   = 30
FINAL_K   = 4
MAX_TURNS = 30  # keep chat history light

# ── Helpers ──────────────────────────────────────────────────────────────────
SEC_PAT = re.compile(r"^(Section|Article|Clause|§)\s+\d+[\w.\-]*", re.I)

def split_legal(text: str) -> List[str]:
    lines, buf, out = text.splitlines(), [], []
    for ln in lines:
        if SEC_PAT.match(ln) and buf:
            out.append("\n".join(buf)); buf = [ln]
        else: buf.append(ln)
    if buf: out.append("\n".join(buf))
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SZ, chunk_overlap=CHUNK_OV)
    chunks = []
    for part in out: chunks.extend(splitter.split_text(part))
    return chunks

def safe_docx_loader(path: str):
    """
    Return a loader **only if** the file is a valid DOCX ZIP archive.
    Otherwise raise a ValueError.
    """
    # A real DOCX is just a ZIP with XML parts.
    if zipfile.is_zipfile(path):
        return Docx2txtLoader(path)
    raise ValueError("Not a valid .docx file")

LOADER_MAP = {
    "pdf":  PyPDFLoader,  "docx": safe_docx_loader, "doc":  TextLoader,  # treat old .doc as plain text fallback
    "pptx": UnstructuredPowerPointLoader, "csv":  CSVLoader, "txt":  TextLoader,
    "png":  UnstructuredImageLoader,     "jpg":  UnstructuredImageLoader,  "jpeg": UnstructuredImageLoader,
}

def load_and_split(path: str) -> List[Document]:
    ext = path.lower().split(".")[-1]
    loader_cls = LOADER_MAP.get(ext)
    if not loader_cls:
        return []

    try:
        # if loader_cls is a function (safe_docx_loader) call it first
        loader = loader_cls(path) if callable(loader_cls) else loader_cls(path)
        docs = loader.load()
    except Exception as e:
        st.warning(f"⚠️  Skipped “{os.path.basename(path)}” – {e}")
        return []

    out = []
    for d in docs:
        meta = d.metadata or {}
        meta["source_file"] = os.path.basename(path)
        for chunk in split_legal(d.page_content):
            out.append(Document(page_content=chunk, metadata=meta))
    return out


@st.cache_resource(show_spinner=False)
def embeddings(): return OpenAIEmbeddings(model=EMBED_MODEL)

def build_or_load_index(corpus: List[Document]):
    if os.path.exists(INDEX_DIR):
        try: return FAISS.load_local(INDEX_DIR, embeddings(), allow_dangerous_deserialization=True)
        except Exception: pass
    vs = FAISS.from_documents(corpus, embeddings())
    vs.save_local(INDEX_DIR); return vs

@st.cache_resource(show_spinner=False)
def get_cross_encoder():
    if CrossEncoder is None: return None
    try: return CrossEncoder("mixedbread-ai/mxbai-rerank-base-v1")
    except Exception: return None

def hybrid_retriever(vs: FAISS, docs: List[Document]):
    dense = vs.as_retriever(search_kwargs={"k": FIRST_K})
    if not (BM25Retriever and EnsembleRetriever): return dense
    bm25 = BM25Retriever.from_texts([d.page_content for d in docs])
    return EnsembleRetriever(retrievers=[dense, bm25], weights=[0.7, 0.3])

def rerank(q: str, docs: List[Document]):
    ce = get_cross_encoder()
    if not ce or not docs: return docs[:FINAL_K]
    scores = ce.predict([[q, d.page_content] for d in docs])
    for d,s in zip(docs,scores): d.metadata["ce"]=float(s)
    return sorted(docs,key=lambda d:d.metadata["ce"],reverse=True)[:FINAL_K]

def ocr_bytes(b:bytes)->str:
    try: return pytesseract.image_to_string(Image.open(io.BytesIO(b)),lang='eng',config='--psm 6')
    except Exception: return ""

def to_dict(m): return {"role":"user" if isinstance(m,HumanMessage) else "system","content":m.content}

# ── UI ───────────────────────────────────────────────────────────────────────
st.set_page_config("Giulia's (🐀) Law AI Assistant", "⚖️")
 
st.markdown("""
<style>
/* stretch content edge-to-edge */
section.main > div { max-width: 1200px; }

/* info-panel look */
.info-panel {
  padding:24px 28px;
  border-radius:14px;
  font-size:1.05rem;
  line-height:1.7;
}
html[data-theme="light"] .info-panel{
  background:#e7f3fc; color:#184361;
  border-left:7px solid #2574a9;
  box-shadow:0 1px 8px #eef4fa;
}
html[data-theme="dark"]  .info-panel{
  background:#2b2b2b; color:#ddd;
  border-left:7px solid #bb86fc;
  box-shadow:0 1px 8px rgba(0,0,0,.5);
}
html[data-theme="dark"]  .info-panel b{color:#fff}
html[data-theme="dark"]  .info-panel a{color:#a0d6ff}
</style>
""", unsafe_allow_html=True)

st.title("⚖️ Giulia's Law AI Assistant!")

# Sidebar
st.sidebar.header("📂 File Uploads & Additional Info")
with st.sidebar.expander("🎯 Quick Tips (commands & scope)", expanded=False):
    st.markdown("""
| **Command** | **What it Does**               | **Scope**           |
|------------:|--------------------------------|---------------------|
| `remember:` | Store a fact permanently       | Across sessions     |
| `memo:`     | Store a fact this session only | Single session      |
| `role:`     | Set the assistant’s persona    | Single session      |
""", unsafe_allow_html=True)

# ---------------- Sidebar: default_context browser -----------------
with st.sidebar.expander("📁 default_context files", expanded=False):
    if not os.path.exists(CTX_DIR):
        st.write("_Folder does not exist yet_")
    else:
        files = sorted(os.listdir(CTX_DIR))
        if not files:
            st.write("_Folder is empty_")
        else:
            for fn in files:
                col1, col2, col3 = st.columns([4, 1, 1])
                col1.write(fn)
                # download link
                with open(os.path.join(CTX_DIR, fn), "rb") as f:
                    col2.download_button(
                        label="⬇️",
                        data=f,
                        file_name=fn,
                        mime="application/octet-stream",
                        key=f"dl_{fn}",
                    )
                # delete button
                if col3.button("🗑️", key=f"del_{fn}"):
                    os.remove(os.path.join(CTX_DIR, fn))
                    # drop the index so it's rebuilt without the file
                    shutil.rmtree(INDEX_DIR, ignore_errors=True)
                    st.experimental_rerun()


uploaded_docs = st.sidebar.file_uploader("Upload legal docs", type=list(LOADER_MAP.keys()), accept_multiple_files=True)
image_file    = st.sidebar.file_uploader("Optional image / chart", type=["png","jpg","jpeg"])
if st.sidebar.button("💾 Save uploads to default_context"):
    if uploaded_docs:
        os.makedirs(CTX_DIR, exist_ok=True)
        for uf in uploaded_docs:
            dest = os.path.join(CTX_DIR, uf.name)
            with open(dest,"wb") as out: out.write(uf.getbuffer())
        shutil.rmtree(INDEX_DIR, ignore_errors=True)
        st.success("Files saved! Reload to re-index.")
    else: st.info("No docs to save.")

# --- Sidebar: narrow or prioritise docs ---------------------------------
all_files = sorted(os.listdir(CTX_DIR)) if os.path.exists(CTX_DIR) else []

sel_docs = st.sidebar.multiselect(
    "📑 Select docs to focus on (optional)", 
    all_files
)

mode = st.sidebar.radio(
    "↳ How should I use the selected docs?",
    ["Prioritise (default)", "Only these docs"],
    horizontal=True
)

# ---------- live resource meter ------------------------------------------
proc = psutil.Process(os.getpid())
rss_mb = proc.memory_info().rss / 1024**2         # RAM in MB
vm      = psutil.virtual_memory()

# size of default_context + faiss_store
def folder_size(path):
    return sum(f.stat().st_size for f in os.scandir(path) if f.is_file())

ctx_bytes  = folder_size(CTX_DIR)   if os.path.exists(CTX_DIR)   else 0
idx_bytes  = folder_size(INDEX_DIR) if os.path.exists(INDEX_DIR) else 0
disk_total, disk_used, _ = shutil.disk_usage(".")

st.sidebar.markdown("## 📊 Resource usage")
st.sidebar.write(
    f"**RAM** {rss_mb:,.0f} MB ({vm.percent:.0f} %)")
st.sidebar.write(
    f"**Docs** {humanize.naturalsize(ctx_bytes)}  \n"
    f"**Index** {humanize.naturalsize(idx_bytes)}")
st.sidebar.write(
    f"**Disk used** {humanize.naturalsize(disk_used)} "
    f"of {humanize.naturalsize(disk_total)}")

# --------------- Sidebar: light-hearted disclaimer -----------------
with st.sidebar.expander("⚖️ Disclaimer", expanded=False):
    st.markdown(
        """
I’m an AI study buddy, **not** your solicitor or lecturer.  
By using this tool you agree that:

* I might be wrong, out-of-date, or miss a key authority.
* Your exam results remain **your** responsibility.
* If you flunk, you’ve implicitly waived all claims in tort, contract,
  equity, and any other jurisdiction you can think of&nbsp;😉

In short: double-check everything before relying on it.
""",
        unsafe_allow_html=True,
    )

with st.expander("ℹ️  How this assistant works", expanded=True):
    st.markdown(
        """
<div class="info-panel">

**📚 Quick overview**

<ul style="margin-left:1.1em;margin-top:12px">

  <li><b>Document-only answers</b> – I rely <em>solely</em> on the files you upload or facts you store with remember/memo or user queries. No web searching!.</li>

  <li><b>Citations</b> – every legal rule or fact ends with a tag such as [#3].  
      A yellow badge appears if something looks uncited.</li>
      

  <li><b>Uploads</b>
      <ul>
        <li><b>Session-only</b> – drag files into the sidebar. They vanish when you refresh.</li>
        <li><b>Keep forever</b> – after uploading, click <strong>“💾 Save uploads”</strong>.  
            Need to remove one later? Use the <strong>🗑️</strong> icon in the sidebar list.</li>
      </ul>
  </li>

  <li><b>Images (beta)</b> – PNG / JPG diagrams are OCR’d. Very small or handwritten text may mis-read.</li>

  <li><b>Limits &amp; tips</b>
      <ul>
        <li>Handles ≈ 4000 text chunks (about 350 average docs) comfortably.</li>
      </ul>
  </li>
  
  <li>📌 <b>Prioritise docs</b> – use the sidebar checklist to tell the assistant which
    files matter most for this question. I’ll look there first, then widen the net.</li>

</ul>

**Pro tip ✨**  Type "show snippet [#2]" and I’ll reveal the exact passage I used.

</div>
        """,
        unsafe_allow_html=True,
    )

query = st.chat_input("Ask anything")

for k, d in {
    "perm": [], "sess": [], "persona": None,
    "hist": [], "last_img": None,
    "user_snips": []          # NEW
}.items():
    st.session_state.setdefault(k, d)

if image_file is not None: st.session_state["last_img"]=image_file
img_file = image_file or st.session_state["last_img"]

# ── Build / load index once ────────────────────────────────────────────────
base_docs=[]
if os.path.exists(CTX_DIR):
    for f in os.listdir(CTX_DIR):
        base_docs.extend(load_and_split(os.path.join(CTX_DIR,f)))
vs = build_or_load_index(base_docs)

LEGAL_KEYWORDS = {
    # words / phrases that usually indicate a rule, authority, or factual assertion
    "held", "holding", "ratio", "rule", "principle", "because", "therefore",
    "thus", "however", "applies", "application", "statute", "section", "§",
    "case", "authority", "precedent", "duty", "breach", "liable", "liability",
    "defence", "defense", "test", "standard", "requirement", "requires",
    "must", "shall", "may", "where", "if", "unless"
}

CITE_PAT   = re.compile(r"\[#\d+\]")
ALPHA_PAT  = re.compile(r"[A-Za-z]")          # ignore empty punctuation blobs

def uncited_substantive(text: str) -> list[str]:
    """
    Flag sentences that look like real legal content but have no [#] citation.
    • Legal keyword  → must cite
    • Long sentence (>25 words) → must cite
    Casual greetings or short factual bios no longer trigger a warning.
    """
    from nltk.tokenize import sent_tokenize

    offenders = []
    for sent in sent_tokenize(text):
        s = sent.strip()
        if not ALPHA_PAT.search(s):                 # punctuation / emoji only
            continue
        if CITE_PAT.search(s):                      # already cited
            continue
        words = len(s.split())
        if words > 25 or any(k in s.lower() for k in LEGAL_KEYWORDS):
            offenders.append(s)
    return offenders

# ── MAIN LOOP ──────────────────────────────────────────────────────────────
if query:
    txt=query.strip(); low=txt.lower()

    if not txt.lower().startswith(("remember:", "memo:", "role:")):
        st.session_state.user_snips.append(txt)
        st.session_state.user_snips = st.session_state.user_snips[-10:]  # keep last 10

    if low.startswith("remember:"):
        st.session_state.perm.append(txt.partition(":")[2].strip())
        st.session_state.hist.append(("assistant","✅ Remembered.")); st.rerun()

    elif low.startswith("memo:"):
        st.session_state.sess.append(txt.partition(":")[2].strip())
        st.session_state.hist.append(("assistant","🗒️ Noted (session).")); st.rerun()

    elif low.startswith("role:"):
        st.session_state.persona=txt.partition(":")[2].strip()
        st.session_state.hist.append(("assistant",f"👤 Persona → {st.session_state.persona}")); st.rerun()

    else:
        # merge uploaded docs (session-only unless saved)
        extra_docs=[]
        if uploaded_docs:
            tmp = tempfile.mkdtemp()
            extra_docs = []
            for uf in uploaded_docs:
                p = os.path.join(tmp, uf.name)
                open(p, "wb").write(uf.getbuffer())
                extra_docs.extend(load_and_split(p))

            # --- add vectors then free RAM ---------------------------------------
            vs.add_documents(extra_docs)          # vectors stay inside FAISS
            del extra_docs
            gc.collect()

        # ---------- Build a retriever honouring the user's choice ---------------
        if sel_docs:
            # helper: match on metadata["source_file"]
            filt = lambda m: m.get("source_file") in sel_docs

            if mode.startswith("Only"):
                # strict filter — anything outside sel_docs is ignored
                hits = vs.as_retriever(search_kwargs={"k": FIRST_K, "filter": filt}).invoke(txt)

            else:  # "Prioritise"
                # 1)   search inside the selected files
                pri_hits = vs.as_retriever(search_kwargs={"k": FIRST_K, "filter": filt}).invoke(txt)
                # 2)   search the whole corpus
                base_hits = vs.as_retriever(search_kwargs={"k": FIRST_K}).invoke(txt)
                # 3)   keep pri_hits first, then append the rest (no duplicates)
                hits = pri_hits + [d for d in base_hits if d not in pri_hits]
        else:
            # no filter at all
            hits = vs.as_retriever(search_kwargs={"k": FIRST_K}).invoke(txt)

        # ---------- optional cross-encoder re-rank, final trim -------------------
        docs = rerank(txt, hits)[:FINAL_K]

        # snippet build
        snippets=[]
        for i,d in enumerate(docs,1):
            snippets.append(f"[#{i}] ({d.metadata.get('source_file','doc')}) {re.sub(r'\\s+',' ',d.page_content)[:1000]}")

        # user snippets
        offset = len(snippets)
        for i, us in enumerate(st.session_state.user_snips, 1):
            snippets.append(f"[#U{i}] (user) {us}")
        
        # image branch
        ocr=""; img_payload=None
        if img_file:
            b=img_file.getvalue(); ocr=ocr_bytes(b)
            img_payload={"type":"image_url","image_url":{"url":f"data:image/png;base64,{base64.b64encode(b).decode()}"}}

    # ── Prompt -----------------------------------------------------------------
    prompt = """
    You are Giulia’s friendly but meticulous law-exam assistant.

    GROUND RULES
    • Your knowledge source hierarchy, in order of authority:  
    1. **Provided Snippets** (numbered [#n]).  
    2. **Stored facts** added with remember:/memo:.  
    3. Generally known public facts *only* if obviously harmless
        (e.g., “LSE stands for London School of Economics”).  
    • Every sentence that states a legal rule, holding, statute section, date,
    or anything that might be challenged in an exam answer must end with its
    citation [#n].  
    • If the necessary information is not present in 1 or 2, respond exactly with:  
    “I don’t have enough information in the provided material to answer that.”

    STYLE
    1. Begin with one conversational line that restates the user’s question.  
    2. Give a detailed, logically structured answer (IRAC only if the user asks).  
    3. Explain legal jargon in plain English.  
    5. Keep tone peer-to-peer, confident, concise.

    (NO CITATION ⇒ NO CLAIM.)
    """.strip()

    # ── Final prompt (add persona if any) ───────────────────────────────
    if st.session_state.persona:
        prompt += f" Adopt persona: {st.session_state.persona}."

    # ── Build message list ──────────────────────────────────────────────
    msgs: List[Union[SystemMessage, HumanMessage]] = [SystemMessage(content=prompt)]

    if snippets:
        msgs.append(SystemMessage(content="Snippets:\n" + "\n\n".join(snippets)))

    for fact in st.session_state.perm + st.session_state.sess:
        msgs.append(SystemMessage(content=f"Fact: {fact}"))

    if ocr.strip():
        msgs.append(SystemMessage(content=f"OCR:\n{ocr.strip()}"))

    # user message (multimodal if image present)
    user_payload = [{"type": "text", "text": txt}, img_payload] if img_payload else txt
    msgs.append(HumanMessage(content=user_payload))

    # ── Call the model ──────────────────────────────────────────────────
    with st.spinner("Thinking …"):
        res = client.chat.completions.create(
            model       = LLM_MODEL,
            messages    = [to_dict(m) for m in msgs],
            temperature = 0.0,
            max_tokens  = 800,
        )

    answer = res.choices[0].message.content.strip()

    if snippets and len(snippets) == 1 and not CITE_PAT.search(answer):
        # snippets[0] is like "[#3] (source) …", so we pull out "#3"
        answer += f" [{snippets[0].split(']')[0][1:]}]"

    # ── Citation check & refusal ────────────────────────────────────────
    missing = uncited_substantive(answer)
    if missing:
        st.warning("⚠️ Sentences without citations: " + " | ".join(missing[:3]))


    # ── Update chat history  ──────────────────────────────────────────────
    # 1)  which snippets were *actually cited*?
    used_tags = re.findall(r"\[#(\d+|U\d+|F\d+)\]", answer)          # ⇢ ['1', 'U1', …]

    tag_to_snip = {
        sn.split("]", 1)[0][2:]: sn           # key = 1 / U3 / …
        for sn in snippets
    }
    used_snippets = [tag_to_snip[t] for t in used_tags if t in tag_to_snip]

    # 2)  append bubbles as dicts
    st.session_state.hist.append(
        {"role": "user", "msg": txt, "sources": []}
    )

    st.session_state.hist.append(
        {
            "role":    "assistant",
            "msg":     answer,
            "sources": used_snippets,          # <- only those with a matching tag
        }
    )

    # keep last N turns
    st.session_state.hist = st.session_state.hist[-MAX_TURNS * 2:]

    # ── Render bubbles  ───────────────────────────────────────────────────
    for entry in st.session_state.hist:
        with st.chat_message(entry["role"]):
            st.write(entry["msg"])

            if entry["role"] == "assistant" and entry["sources"]:
                legend = []
                for sn in entry["sources"]:
                    tag = sn.split("]", 1)[0][1:]                 # "#1" / "#U2"
                    try:
                        fname = sn.split("(", 1)[1].split(")", 1)[0]
                    except IndexError:
                        fname = "source"
                    legend.append(f"**{tag}**  →  {fname}")

                with st.expander("📑 Sources used"):
                    st.markdown("\n".join(legend))