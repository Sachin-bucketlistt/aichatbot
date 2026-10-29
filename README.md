# AI Chatbot 🤖✨

An enterprise-ready, modular, and configurable AI Chatbot powered by FastAPI, LiteLLM, Weaviate RAG, Model Context Protocol (MCP), and React (Vite).

Designed to be repurposed for any domain (customer FAQ, internal policy, travel assistant, product support) without touching code—simply by updating the environment configuration and knowledge base.

---

## 🌟 Key Features

- **⚡ Real-time Token Streaming**: Server-Sent Events (SSE) for buttery-smooth streaming responses.
- **🔄 Model & Provider Agnostic**: Powered by [LiteLLM](https://github.com/BerriAI/litellm). Switch seamlessly between OpenAI (`gpt-4o-mini`, `gpt-4o`), Anthropic (`claude-3-5-sonnet`), Google Gemini, Ollama, and more by changing a single `.env` variable.
- **🧠 Hybrid RAG Search**: 
  - **Weaviate Vector Search**: High-performance semantic vector retrieval.
  - **Fallback Context**: Automatically falls back to flat markdown prompt context when vector DB is unpopulated.
- **🔌 Model Context Protocol (MCP) Integration**: Connects to live tools and remote MCP servers (e.g. `bucketlistt` catalog API).
- **📝 Automated Lead Capture**: Built-in interactive modal and tools to collect customer lead details automatically during chat flows.
- **🎨 Modern React UI**: Built with Vite, React, TypeScript, and clean CSS featuring quick-response chips, message markdown rendering, light/dark responsive styling, and sidebar controls.
- **🐳 Docker Compose Ready**: One-command local development setup for Weaviate, FastAPI Backend, and Nginx Frontend.

---

## 🏗️ Architecture & Data Flow

```
┌──────────────────────────────────────────────────────────┐
│              Browser (Vite + React Frontend)            │
│               - SSE Token Stream Listener                │
│               - Interactive Quick Chips & Modals         │
└────────────────────────────┬─────────────────────────────┘
                             │ POST /api/chat (SSE Stream)
                             ▼
┌──────────────────────────────────────────────────────────┐
│                 FastAPI Backend Service                  │
│               - Config & Env Resolution                  │
│               - Tool Execution & RAG Integration         │
└───────┬────────────────────┬──────────────────────┬──────┘
        │                    │                      │
        ▼                    ▼                      ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│   LiteLLM    │     │   Weaviate   │     │ Remote MCP Server│
│(OpenAI/Claude│     │  Vector DB   │     │  (Catalog Tools) │
│ /Gemini/etc.)│     │  (RAG Index) │     │                  │
└──────────────┘     └──────────────┘     └──────────────────┘
```

---

## 📁 Project Structure

```
.
├── ARCHITECTURE.md          # Detailed system design & decisions
├── FUNCTIONALITY.md         # Full feature specifications & user flows
├── docker-compose.yml       # Orchestrates Weaviate, Backend, and Frontend
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI application & SSE endpoints
│   │   ├── llm.py           # LiteLLM client & completion streaming logic
│   │   ├── retriever.py     # Weaviate RAG vector retrieval & fallback
│   │   ├── mcp_client.py   # MCP server client & remote tools handler
│   │   ├── tools.py         # Native tool functions & lead capture
│   │   ├── config.py        # Environment variables & settings schema
│   │   └── schemas.py       # Pydantic request/response models
│   ├── data/
│   │   ├── knowledge_base.md        # Base text knowledge base
│   │   └── bucketlistt_scraped.json # Scraped JSON dataset
│   ├── scripts/             # Data scraping & Weaviate indexing scripts
│   ├── tests/               # Backend pytest unit and integration tests
│   └── Dockerfile
└── frontend/
    ├── src/
    │   ├── api/             # API layer (SSE stream parsing)
    │   ├── components/      # Header, Sidebar, MessageContent, LeadModal, etc.
    │   ├── App.tsx          # Main chat interface component
    │   └── index.css        # Core styling & modern CSS variables
    └── Dockerfile
```

---

## 🚀 Quick Start

### Option 1: Docker Compose (Recommended)

Run the entire application stack (Weaviate + Backend + Frontend) with Docker:

1. **Clone the repository**:
   ```bash
   git clone https://github.com/okghumo/aichatbot.git
   cd aichatbot
   ```

2. **Set up Environment Variables**:
   ```bash
   cp backend/.env.example backend/.env
   cp frontend/.env.example frontend/.env
   ```
   Add your LLM API Key (e.g. `OPENAI_API_KEY`) in `backend/.env`.

3. **Launch Stack**:
   ```bash
   docker-compose up --build
   ```

4. **Access Applications**:
   - **Frontend**: [http://localhost:5173](http://localhost:5173)
   - **Backend API**: [http://localhost:8000](http://localhost:8000)
   - **API Docs (Swagger)**: [http://localhost:8000/docs](http://localhost:8000/docs)
   - **Weaviate DB**: [http://localhost:8080](http://localhost:8080)

---

### Option 2: Local Development Setup

#### Backend Setup (Python 3.12+)

```bash
cd backend

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt # or uv / pip install .

# Copy environment variables
cp .env.example .env

# Run FastAPI server
uvicorn app.main:app --reload --port 8000
```

#### Frontend Setup (Node.js 18+)

```bash
cd frontend

# Install dependencies
npm install

# Copy environment variables
cp .env.example .env

# Run Vite development server
npm run dev
```

---

## ⚙️ Configuration Guide

Key environment variables in `backend/.env`:

| Variable | Description | Default |
|---|---|---|
| `OPENAI_API_KEY` | API Key for OpenAI or provider of choice | `sk-...` |
| `LLM_MODEL` | LiteLLM model identifier (`gpt-4o-mini`, `claude-3-5-sonnet-20241022`, etc.) | `gpt-4o-mini` |
| `SYSTEM_PROMPT_FILE` | Relative path to knowledge base file | `data/knowledge_base.md` |
| `WEAVIATE_URL` | Vector DB URL for semantic RAG | `http://localhost:8080` |
| `MCP_SERVER_URL` | Optional remote MCP tool server endpoint | `https://mcp.bucketlistt.com/mcp` |
| `CORS_ORIGINS` | Allowed frontend origins | `http://localhost:5173` |

---

## 🧪 Testing

### Backend Unit & Integration Tests

```bash
cd backend
pytest
```

### Frontend Tests

```bash
cd frontend
npm run test
```

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
