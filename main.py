import streamlit as st
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import TextLoader, PyPDFLoader
from langchain_core.messages import SystemMessage, HumanMessage
from dotenv import load_dotenv
import os
import tempfile

# ─── Load env variables ────────────────────────────────────────────────────────
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

# ─── Initialize session state ─────────────────────────────────────────────────
if "memory_facts" not in st.session_state:
    st.session_state.memory_facts = []
if "session_facts" not in st.session_state:
    st.session_state.session_facts = []
if "persona" not in st.session_state:
    st.session_state.persona = None
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

# ─── Helpers: load & index docs ───────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def load_and_index_defaults(folder="default_context"):
    """
    Load all .pdf/.txt files from folder and build a FAISS index and docs list.
    Returns tuple: (docs_list, FAISS_index)
    """
    docs = []
    if os.path.exists(folder):
        for fname in os.listdir(folder):
            if not fname.lower().endswith((".pdf", ".txt")):
                continue
            path = os.path.join(folder, fname)
            loader = PyPDFLoader(path) if fname.lower().endswith(".pdf") else TextLoader(path)
            docs.extend(loader.load())
    embeddings = OpenAIEmbeddings(api_key=api_key)
    faiss_index = FAISS.from_documents(docs, embeddings)
    return docs, faiss_index


def load_uploaded_files(uploaded_files):
    """Load user-uploaded pdf/txt files into LangChain documents (in-memory only)."""
    if not uploaded_files:
        return []
    tmp = tempfile.mkdtemp()
    docs = []
    for f in uploaded_files:
        fp = os.path.join(tmp, f.name)
        with open(fp, "wb") as out:
            out.write(f.getbuffer())
        loader = PyPDFLoader(fp) if f.name.lower().endswith(".pdf") else TextLoader(fp)
        docs.extend(loader.load())
    return docs


def build_vectorstore(default_docs, default_index, session_docs):
    """
    If user has uploaded session_docs, rebuild a temporary FAISS index over default_docs+session_docs.
    Otherwise return the cached default_index.
    """
    if session_docs:
        embeddings = OpenAIEmbeddings(api_key=api_key)
        return FAISS.from_documents(default_docs + session_docs, embeddings)
    return default_index

# ─── Page config & title ─────────────────────────────────────────────────────
st.set_page_config(page_title="Giulia's Law AI Assistant", page_icon="🤖")
st.title("🤖 Giulia's Law AI Assistant")

# ─── Sidebar: uploader & mode toggle ───────────────────────────────────────────
st.sidebar.header("📂 File Uploads")

upload_mode = st.sidebar.radio(
    "Upload scope:",
    ("Session only", "Persist across sessions"),
    index=0
)

inline_files = st.sidebar.file_uploader(
    "Upload documents",
    type=["pdf", "txt"],
    accept_multiple_files=True,
    key="inline_uploader"
)

# If Persist mode, copy into default_context so they get re-indexed next session
if upload_mode == "Persist across sessions" and inline_files:
    os.makedirs("default_context", exist_ok=True)
    for f in inline_files:
        dest = os.path.join("default_context", f.name)
        if not os.path.exists(dest):
            with open(dest, "wb") as out:
                out.write(f.getbuffer())
    st.sidebar.success("✅ Files saved permanently. They’ll reload next time.")

# ─── Introductory info box ────────────────────────────────────────────────────
st.markdown("""
<div style='margin-bottom:24px; padding:26px 28px; background:#e7f3fc; border-radius:14px; border-left:7px solid #2574a9; color:#184361; font-size:1.08rem; box-shadow:0 1px 8px #eef4fa; line-height:1.7;'>
<b style='font-size:1.13rem;'>ℹ️ This assistant ONLY uses information from your uploaded documents and <span style='color:#1c853b;'>preloaded default context</span> (e.g., your CV & course info—already included).</b>
<ul style='margin-left:1.1em; margin-top:12px;'>
    <li>If the answer is <b>not present</b> in your documents or context, it will let you know.</li>
    <li style='margin-top:8px;'><span style='color:#d97706; font-weight:600;'>It will <u>not</u> invent any information.</span></li>
    <li style='margin-top:8px;'>You can upload multiple files at once; their content is <b>combined</b> for answering.</li>
</ul>
<b>✨ Tip:</b> Upload documents that contain the details you want to ask about.
</div>
""", unsafe_allow_html=True)

# ─── Quick Tips (sidebar expander) ────────────────────────────────────────────
with st.sidebar.expander("🎯 Quick Tips (commands & scope)", expanded=False):
    st.markdown("""
| **Command**   | **What it Does**                     | **Scope**           |
|--------------:|--------------------------------------|---------------------|
| `remember:`   | Store a fact **permanently**         | Across all sessions |
| `memo:`       | Store a fact **for this session**    | Single session      |
| `role:`       | Set your assistant’s **persona/role**| N/A                 |
""", unsafe_allow_html=True)

# ─── Build vector store ───────────────────────────────────────────────────────
default_docs, default_index = load_and_index_defaults()
session_docs = load_uploaded_files(inline_files)
vector_store = build_vectorstore(default_docs, default_index, session_docs)
retriever = vector_store.as_retriever()
llm = ChatOpenAI(api_key=api_key, model="gpt-4o-mini", temperature=0.0)

# ─── Handle user input & chat ────────────────────────────────────────────────
user_input = st.chat_input("Type a question or use `remember:`, `memo:`, `role:`…")
if user_input:
    txt = user_input.strip()
    low = txt.lower()

    # ─── Commands ────────────────────────────────────────────────────────────
    if low.startswith("remember:"):
        st.session_state.memory_facts.append(txt.split(":", 1)[1].strip())
        st.success("✅ Fact remembered permanently.")
    elif low.startswith("memo:"):
        st.session_state.session_facts.append(txt.split(":", 1)[1].strip())
        st.info("ℹ️ Session-only fact added.")
    elif low.startswith("role:"):
        st.session_state.persona = txt.split(":", 1)[1].strip()
        st.success(f"👤 Persona set to: {st.session_state.persona}")
    else:
        # ─── Normal LLM query ────────────────────────────────────────────────────
        docs = retriever.invoke(txt)
        context = "\n\n".join(d.page_content for d in docs)

        sys_prompt = (
            "You are a helpful legal assistant. Answer strictly using the provided "
            "context, remembered facts, session facts, and any facts stated inline. "
            "Do not invent info."
        )
        if st.session_state.persona:
            sys_prompt += f" Adopt persona: {st.session_state.persona}."

        messages = [SystemMessage(content=sys_prompt)]
        if context:
            messages.append(SystemMessage(content=f"Context:\n{context}"))
        for f in st.session_state.memory_facts:
            messages.append(SystemMessage(content=f"Remembered fact: {f}"))
        for f in st.session_state.session_facts:
            messages.append(SystemMessage(content=f"Session fact: {f}"))
        messages.append(HumanMessage(content=txt))

        if not (docs or st.session_state.memory_facts or st.session_state.session_facts):
            st.warning("⚠️ Not enough info to answer your request.")
        else:
            resp = llm.invoke(messages)
            st.session_state.chat_history.append(("User", txt))
            st.session_state.chat_history.append(("Assistant", resp.content))

# ─── Render chat history ───────────────────────────────────────────────────────
for speaker, text in st.session_state.chat_history:
    role = "user" if speaker == "User" else "assistant"
    st.chat_message(role).write(text)