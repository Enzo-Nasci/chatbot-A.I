// ============================================================
// Configuração
// ============================================================
// Ajuste para a URL onde o FastAPI está rodando.
const API_BASE = "http://localhost:8000";
const CHAT_ENDPOINT = `${API_BASE}/chat`;

// ============================================================
// Elementos
// ============================================================
const thread = document.getElementById("thread");
const threadEmpty = document.getElementById("thread-empty");
const composer = document.getElementById("composer");
const inputPergunta = document.getElementById("pergunta");
const botaoEnviar = document.getElementById("enviar");
const sessionIdEl = document.getElementById("session-id");
const resetButton = document.getElementById("reset-session");
const statusDot = document.getElementById("status-dot");
const hint = document.getElementById("hint");

// ============================================================
// Sessão
// ============================================================
const SESSION_STORAGE_KEY = "assistente_session_id";
const USER_STORAGE_KEY = "assistente_user_id";
const USER_ID_PADRAO = "usuario_teste";

function gerarSessionId() {
  if (window.crypto && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return "sess-" + Math.random().toString(36).slice(2, 10);
}

function obterOuCriarSessionId() {
  let id = localStorage.getItem(SESSION_STORAGE_KEY);
  if (!id) {
    id = gerarSessionId();
    localStorage.setItem(SESSION_STORAGE_KEY, id);
  }
  return id;
}

function obterUserId() {
  let id = localStorage.getItem(USER_STORAGE_KEY);
  if (!id) {
    id = USER_ID_PADRAO;
    localStorage.setItem(USER_STORAGE_KEY, id);
  }
  return id;
}

async function iniciarNovaSessao() {
  const sessaoAnterior = sessionId;
  try {
    await fetch(`${API_BASE}/sessao/${sessaoAnterior}/encerrar`, { method: "POST" });
  } catch (erro) {
    console.error(erro);
  }

  const id = gerarSessionId();
  sessionId = id;
  localStorage.setItem(SESSION_STORAGE_KEY, id);
  exibirSessionId(id);
  thread.innerHTML = "";
  thread.appendChild(threadEmpty);
  threadEmpty.style.display = "block";
  setHint("Nova sessão iniciada.");
}

function exibirSessionId(id) {
  sessionIdEl.textContent = id.length > 12 ? id.slice(0, 8) + "…" : id;
  sessionIdEl.title = id;
}

let sessionId = obterOuCriarSessionId();
const userId = obterUserId();
exibirSessionId(sessionId);

resetButton.addEventListener("click", iniciarNovaSessao);

// ============================================================
// Utilidades de UI
// ============================================================
function setHint(texto, comoErro = false) {
  hint.textContent = texto || "";
  hint.classList.toggle("is-error", comoErro);
}

function marcarStatus(online) {
  statusDot.classList.toggle("is-offline", !online);
}

function escaparHtml(texto) {
  const div = document.createElement("div");
  div.textContent = texto;
  return div.innerHTML;
}

function rolarParaFinal() {
  thread.scrollTop = thread.scrollHeight;
}

function adicionarMensagem({ tipo, texto, agentes }) {
  threadEmpty.style.display = "none";

  const wrapper = document.createElement("div");
  wrapper.className = `message message--${tipo}`;

  const label = document.createElement("div");
  label.className = "message__label";
  label.textContent = tipo === "user" ? "você" : tipo === "error" ? "erro" : "assistente";
  wrapper.appendChild(label);

  const bubble = document.createElement("div");
  bubble.className = "message__bubble";
  bubble.innerHTML = escaparHtml(texto);
  wrapper.appendChild(bubble);

  if (agentes && agentes.length > 0) {
    const agentesEl = document.createElement("div");
    agentesEl.className = "message__agents";
    agentes.forEach((nome) => {
      const tag = document.createElement("span");
      tag.className = "agent-tag";
      tag.textContent = nome;
      agentesEl.appendChild(tag);
    });
    wrapper.appendChild(agentesEl);
  }

  thread.appendChild(wrapper);
  rolarParaFinal();
  return wrapper;
}

function adicionarIndicadorDigitando() {
  const wrapper = document.createElement("div");
  wrapper.className = "message message--assistant";
  wrapper.id = "typing-indicator";

  const label = document.createElement("div");
  label.className = "message__label";
  label.textContent = "assistente";
  wrapper.appendChild(label);

  const bubble = document.createElement("div");
  bubble.className = "message__bubble";
  bubble.innerHTML = `<span class="typing"><span></span><span></span><span></span></span>`;
  wrapper.appendChild(bubble);

  thread.appendChild(wrapper);
  rolarParaFinal();
}

function removerIndicadorDigitando() {
  const el = document.getElementById("typing-indicator");
  if (el) el.remove();
}

// ============================================================
// Textarea: auto-resize + enviar com Enter
// ============================================================
inputPergunta.addEventListener("input", () => {
  inputPergunta.style.height = "auto";
  inputPergunta.style.height = Math.min(inputPergunta.scrollHeight, 140) + "px";
});

inputPergunta.addEventListener("keydown", (evento) => {
  if (evento.key === "Enter" && !evento.shiftKey) {
    evento.preventDefault();
    composer.requestSubmit();
  }
});

// ============================================================
// Envio da pergunta
// ============================================================
composer.addEventListener("submit", async (evento) => {
  evento.preventDefault();

  const pergunta = inputPergunta.value.trim();
  if (!pergunta) return;

  adicionarMensagem({ tipo: "user", texto: pergunta });
  inputPergunta.value = "";
  inputPergunta.style.height = "auto";
  setHint("");

  botaoEnviar.disabled = true;
  inputPergunta.disabled = true;
  adicionarIndicadorDigitando();

  try {
    const resposta = await fetch(CHAT_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pergunta: pergunta,
        session_id: sessionId,
        user_id: userId,
      }),
    });

    if (!resposta.ok) {
      const detalhe = await resposta.text();
      throw new Error(
        `A API respondeu com status ${resposta.status}. ${detalhe || "Sem detalhe no corpo da resposta."}`
      );
    }

    const dados = await resposta.json();
    marcarStatus(true);
    removerIndicadorDigitando();

    adicionarMensagem({
      tipo: "assistant",
      texto: dados.resposta ?? "(resposta vazia)",
      agentes: dados.agentes_chamados,
    });
  } catch (erro) {
    marcarStatus(false);
    removerIndicadorDigitando();
    adicionarMensagem({
      tipo: "error",
      texto: erro.message
    });
    setHint(erro.message, true);
    console.error(erro);
  } finally {
    botaoEnviar.disabled = false;
    inputPergunta.disabled = false;
    inputPergunta.focus();
  }
});


const btnHistorico = document.getElementById("btn-historico");

// ============================================================
// Busca de Histórico
// ============================================================
btnHistorico.addEventListener("click", async () => {
  try {
    setHint("Buscando histórico...");
    btnHistorico.disabled = true; // Evita múltiplos cliques

    // Faz a requisição para a nova rota que criamos no FastAPI
    const resposta = await fetch(`${API_BASE}/historico/${encodeURIComponent(userId)}`);

    if (!resposta.ok) {
      throw new Error(`Erro ao buscar histórico: ${resposta.status}`);
    }

    const dados = await resposta.json();
    
    // Verifica se existem sessões anteriores
    if (dados.historico && dados.historico.length > 0) {
      let textoHistorico = "<strong>📜 Resumo das suas conversas anteriores:</strong><br><br>";
      
      // Monta o texto iterando pelos resumos que vieram do banco
      dados.historico.forEach(sessao => {
        const dataFmt = new Date(sessao.iniciada_em).toLocaleDateString('pt-BR');
        textoHistorico += `<em>${dataFmt}</em>: ${sessao.resumo}<br><br>`;
      });
      
      // Reaproveita a função existente para mostrar na tela
      adicionarMensagem({ 
        tipo: "assistant", 
        texto: textoHistorico, 
        agentes: ["sistema_memoria"] 
      });
      
      setHint("Histórico carregado com sucesso.");
    } else {
      adicionarMensagem({ 
        tipo: "assistant", 
        texto: "Nenhuma conversa anterior foi encontrada para este usuário.", 
        agentes: ["sistema"] 
      });
      setHint("");
    }
  } catch (erro) {
    setHint("Falha ao carregar histórico.", true);
    console.error(erro);
  } finally {
    btnHistorico.disabled = false;
    rolarParaFinal();
  }
});



// Foco inicial
inputPergunta.focus();
