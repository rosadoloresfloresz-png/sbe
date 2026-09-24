#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""chat-local.py - una pagina para chatear con tu modelo de Kaggle.

No instala nada (solo libreria estandar) y NO toca tu proxy: lee la URL y el TOKEN del archivo
zcode-anthropic-proxy.json, que es el que tu panel ya mantiene actualizado. Asi no hay que copiar
nada a mano: si el panel esta bien, esto funciona.

Uso
---
    python chat-local.py          (o doble clic en chatear.cmd)

y abre en el navegador:

    http://127.0.0.1:8082

Que tiene
---------
- Respuestas en streaming: el texto aparece segun lo escribe el modelo (importante, porque va
  lento: unos pocos tokens por segundo).
- El bloque de razonamiento (`<think>...</think>`) se separa y se puede desplegar, para que no
  tape la respuesta.
- Un punto de estado arriba: si esta en verde, el tunel de Kaggle responde y el token vale.
"""
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(AQUI, "zcode-anthropic-proxy.json")
PUERTO = 8082
MODELO = "razonamiento"


def leer_config():
    """La URL y el TOKEN que tiene en uso tu proxy (el mismo archivo que edita su panel)."""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            datos = json.load(f)
        return datos.get("upstream", "").rstrip("/"), datos.get("token", "")
    except Exception as e:
        print(f"  (aviso: no pude leer {os.path.basename(CONFIG)}: {e})")
        return "", ""


UPSTREAM, TOKEN = "", ""


def comprobar():
    """Pregunta si el tunel responde y si el token vale."""
    if not UPSTREAM:
        return {"ok": False, "mensaje": "No encuentro la URL. Pega la URL y el TOKEN en el panel."}
    peticion = urllib.request.Request(UPSTREAM + "/v1/models",
                                      headers={"X-VS-Token": TOKEN})
    try:
        with urllib.request.urlopen(peticion, timeout=25) as r:
            datos = json.loads(r.read().decode() or "{}")
        nombres = [m.get("id") for m in datos.get("data", [])]
        return {"ok": True, "mensaje": "Conectado con " + UPSTREAM,
                "modelos": nombres, "url": UPSTREAM}
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return {"ok": False, "mensaje": "El tunel responde pero rechaza el token: "
                                            "copia el TOKEN nuevo en el panel."}
        return {"ok": False, "mensaje": f"El tunel responde {e.code}."}
    except Exception as e:
        return {"ok": False,
                "mensaje": f"No responde ({type(e).__name__}): la sesion de Kaggle se cerro. "
                           f"Arranca el motor y pega la URL y el TOKEN nuevos en el panel."}


PAGINA = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chiquita · tu modelo de Kaggle</title>
<style>
  :root { --fondo:#0f1115; --panel:#171a21; --borde:#262b36; --texto:#e6e9ef;
          --gris:#8b93a3; --acento:#4f8cff; --verde:#38d39f; --rojo:#ff6b6b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--fondo); color:var(--texto); font:15px/1.55 system-ui,
         "Segoe UI",Roboto,sans-serif; display:flex; flex-direction:column; height:100vh; }
  header { padding:10px 16px; border-bottom:1px solid var(--borde); display:flex; gap:12px;
           align-items:center; background:var(--panel); }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  #estado { font-size:13px; color:var(--gris); display:flex; align-items:center; gap:7px;
            margin-left:auto; }
  #punto { width:9px; height:9px; border-radius:50%; background:var(--gris); }
  #punto.bien { background:var(--verde); } #punto.mal { background:var(--rojo); }
  #hilo { flex:1; overflow-y:auto; padding:18px 16px; display:flex; flex-direction:column;
          gap:14px; }
  .turno { max-width:820px; width:100%; margin:0 auto; }
  .quien { font-size:12px; color:var(--gris); margin-bottom:4px; }
  .burbuja { background:var(--panel); border:1px solid var(--borde); border-radius:10px;
             padding:10px 13px; white-space:pre-wrap; word-wrap:break-word; }
  .yo .burbuja { background:#1d2430; }
  .pensar { margin-top:8px; font-size:13px; color:var(--gris); }
  .pensar summary { cursor:pointer; }
  .pensar div { border-left:2px solid var(--borde); padding-left:10px; margin-top:6px;
                white-space:pre-wrap; }
  footer { border-top:1px solid var(--borde); background:var(--panel); padding:12px 16px; }
  .caja { max-width:820px; margin:0 auto; display:flex; gap:10px; align-items:flex-end; }
  textarea { flex:1; resize:none; min-height:52px; max-height:190px; padding:10px 12px;
             border-radius:10px; border:1px solid var(--borde); background:#12151b;
             color:var(--texto); font:inherit; }
  button { border:0; border-radius:9px; padding:11px 16px; font:inherit; font-weight:600;
           cursor:pointer; background:var(--acento); color:#fff; }
  button.sec { background:#242a36; color:var(--texto); font-weight:500; }
  button:disabled { opacity:.5; cursor:default; }
  .opciones { max-width:820px; margin:8px auto 0; display:flex; gap:16px; align-items:center;
              font-size:12px; color:var(--gris); flex-wrap:wrap; }
  .opciones label { display:flex; gap:6px; align-items:center; }
  input[type=range] { width:110px; }
  .aviso { max-width:820px; margin:0 auto 8px; font-size:13px; color:#ffd479; }
</style>
</head>
<body>
<header>
  <h1>Chiquita · tu Qwen3 entrenado</h1>
  <div id="estado"><span id="punto"></span><span id="textoEstado">comprobando…</span></div>
</header>

<div id="hilo"></div>

<footer>
  <div class="aviso" id="aviso" hidden></div>
  <div class="caja">
    <textarea id="entrada" placeholder="Escribe y pulsa Enter (Shift+Enter para otra linea)…"
              rows="2"></textarea>
    <button id="enviar">Enviar</button>
    <button id="parar" class="sec" hidden>Parar</button>
    <button id="limpiar" class="sec">Limpiar</button>
  </div>
  <div class="opciones">
    <label>temperatura <input id="temp" type="range" min="0" max="1.2" step="0.05" value="0.6">
      <span id="tempVal">0.60</span></label>
    <label>tokens <input id="tokens" type="number" min="32" max="4096" step="32" value="512"
      style="width:74px;background:#12151b;color:#e6e9ef;border:1px solid #262b36;border-radius:6px;padding:3px 6px"></label>
    <span id="cronometro"></span>
  </div>
</footer>

<script>
const hilo = document.getElementById('hilo');
const entrada = document.getElementById('entrada');
const botonEnviar = document.getElementById('enviar');
const botonParar = document.getElementById('parar');
const cronometro = document.getElementById('cronometro');
let historial = [];        // lo que se manda al modelo
let cortar = null;         // para el boton Parar

// ---------- estado de la conexion ----------
async function mirarEstado() {
  try {
    const r = await fetch('/api/estado');
    const d = await r.json();
    const punto = document.getElementById('punto');
    punto.className = d.ok ? 'bien' : 'mal';
    document.getElementById('textoEstado').textContent = d.mensaje;
  } catch (e) {
    document.getElementById('punto').className = 'mal';
    document.getElementById('textoEstado').textContent = 'sin conexion con el chat local';
  }
}
mirarEstado();
setInterval(mirarEstado, 20000);

// ---------- pintar un turno ----------
function pintar(rol, texto) {
  const turno = document.createElement('div');
  turno.className = 'turno ' + (rol === 'user' ? 'yo' : 'ia');
  const quien = document.createElement('div');
  quien.className = 'quien';
  quien.textContent = rol === 'user' ? 'Tu' : 'Chiquita';
  const burbuja = document.createElement('div');
  burbuja.className = 'burbuja';
  turno.appendChild(quien);
  turno.appendChild(burbuja);
  hilo.appendChild(turno);
  hilo.scrollTop = hilo.scrollHeight;
  return { burbuja, texto: texto };
}

// separa el razonamiento (<think>...</think>) de la respuesta
function repartir(bruto) {
  let pensar = '', responder = bruto;
  const cierre = bruto.indexOf('</think>');
  const abre = bruto.indexOf('<think>');
  if (cierre >= 0) {
    pensar = bruto.slice(abre >= 0 ? abre + 7 : 0, cierre);
    responder = bruto.slice(cierre + 8);
  } else if (abre >= 0) {
    pensar = bruto.slice(abre + 7);
    responder = '';
  }
  return { pensar: pensar.trim(), responder: responder.trim() };
}

function escribir(nodo, bruto) {
  const partes = repartir(bruto);
  nodo.burbuja.textContent = partes.responder || (partes.pensar ? '' : '…');
  const viejo = nodo.burbuja.parentElement.querySelector('.pensar');
  if (viejo) viejo.remove();
  if (partes.pensar) {
    const detalle = document.createElement('details');
    detalle.className = 'pensar';
    const resumen = document.createElement('summary');
    resumen.textContent = 'razonamiento (' + partes.pensar.length + ' caracteres)';
    const cuerpo = document.createElement('div');
    cuerpo.textContent = partes.pensar;
    detalle.appendChild(resumen);
    detalle.appendChild(cuerpo);
    if (!partes.responder) detalle.open = true;
    nodo.burbuja.parentElement.appendChild(detalle);
  }
  hilo.scrollTop = hilo.scrollHeight;
}

// ---------- enviar ----------
async function enviar() {
  const texto = entrada.value.trim();
  if (!texto) return;
  entrada.value = '';
  historial.push({ role: 'user', content: texto });
  pintar('user', texto).burbuja.textContent = texto;

  const nodo = pintar('ia', '');
  const control = new AbortController();
  cortar = control;
  botonEnviar.disabled = true;
  botonParar.hidden = false;
  const inicio = Date.now();
  const reloj = setInterval(() => {
    cronometro.textContent = ((Date.now() - inicio) / 1000).toFixed(1) + ' s';
  }, 200);

  let bruto = '';
  try {
    const respuesta = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: control.signal,
      body: JSON.stringify({
        messages: historial,
        temperature: parseFloat(document.getElementById('temp').value),
        max_tokens: parseInt(document.getElementById('tokens').value, 10),
      }),
    });
    if (!respuesta.ok) {
      const d = await respuesta.json().catch(() => ({}));
      document.getElementById('aviso').hidden = false;
      document.getElementById('aviso').textContent = d.error || ('error ' + respuesta.status);
      nodo.burbuja.textContent = '(no se pudo preguntar)';
      return;
    }
    const lector = respuesta.body.getReader();
    const decodificador = new TextDecoder();
    let resto = '';
    while (true) {
      const { done, value } = await lector.read();
      if (done) break;
      resto += decodificador.decode(value, { stream: true });
      const trozos = resto.split('\n\n');
      resto = trozos.pop();
      for (const trozo of trozos) {
        for (const linea of trozo.split('\n')) {
          if (!linea.startsWith('data: ')) continue;
          const carga = linea.slice(6).trim();
          if (carga === '[DONE]') continue;
          try {
            const evento = JSON.parse(carga);
            const delta = evento.choices?.[0]?.delta?.content;
            if (delta) { bruto += delta; escribir(nodo, bruto); }
          } catch (e) { /* trozo cortado: se ignora */ }
        }
      }
    }
    if (bruto.trim()) historial.push({ role: 'assistant', content: bruto });
  } catch (e) {
    if (e.name !== 'AbortError') {
      document.getElementById('aviso').hidden = false;
      document.getElementById('aviso').textContent = 'se corto: ' + e.message;
    }
  } finally {
    clearInterval(reloj);
    botonEnviar.disabled = false;
    botonParar.hidden = true;
    cortar = null;
    escribir(nodo, bruto);
  }
}

botonEnviar.onclick = enviar;
botonParar.onclick = () => cortar && cortar.abort();
document.getElementById('limpiar').onclick = () => {
  historial = []; hilo.innerHTML = ''; cronometro.textContent = '';
};
entrada.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviar(); }
});
document.getElementById('temp').oninput = (e) => {
  document.getElementById('tempVal').textContent = parseFloat(e.target.value).toFixed(2);
};
mirarEstado();
</script>
</body>
</html>
"""


class Manejador(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, codigo, datos):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/index.html"):
            cuerpo = PAGINA.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        elif self.path.startswith("/api/estado"):
            self._json(200, comprobar())
        else:
            self._json(404, {"error": "no existe"})

    def do_POST(self):
        if not self.path.startswith("/api/chat"):
            return self._json(404, {"error": "no existe"})
        global UPSTREAM, TOKEN
        UPSTREAM, TOKEN = leer_config()          # se relee: si cambias la URL, no hay que reiniciar
        if not UPSTREAM:
            return self._json(400, {"error": "Falta la URL en zcode-anthropic-proxy.json. "
                                             "Pegala en el panel (http://127.0.0.1:8081/panel)."})
        largo = int(self.headers.get("Content-Length") or 0)
        try:
            peticion = json.loads(self.rfile.read(largo) or b"{}")
        except Exception:
            return self._json(400, {"error": "peticion mal formada"})

        cuerpo = json.dumps({
            "model": MODELO,
            "messages": peticion.get("messages", []),
            "temperature": peticion.get("temperature", 0.6),
            "max_tokens": peticion.get("max_tokens", 512),
            "stream": True,
        }).encode()

        pedido = urllib.request.Request(
            UPSTREAM + "/v1/chat/completions", data=cuerpo,
            headers={"Content-Type": "application/json", "X-VS-Token": TOKEN})
        try:
            respuesta = urllib.request.urlopen(pedido, timeout=3600)
        except urllib.error.HTTPError as e:
            detalle = e.read().decode(errors="replace")[:400]
            if e.code == 403:
                mensaje = ("El tunel rechaza el token: la sesion de Kaggle cambio. Copia la URL "
                           "y el TOKEN nuevos en el panel (http://127.0.0.1:8081/panel).")
            else:
                mensaje = f"El modelo respondio {e.code}: {detalle}"
            return self._json(502, {"error": mensaje})
        except Exception as e:
            return self._json(502, {"error": f"No se pudo llegar al tunel ({type(e).__name__}). "
                                             f"Arranca el motor en Kaggle y pega la URL y el "
                                             f"TOKEN en el panel."})

        # se pasa el streaming tal cual, trozo a trozo: importante porque el modelo va lento
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                trozo = respuesta.read(512)
                if not trozo:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(trozo), trozo))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class Servidor(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global UPSTREAM, TOKEN
    UPSTREAM, TOKEN = leer_config()
    try:
        servidor = Servidor(("127.0.0.1", PUERTO), Manejador)
    except OSError:
        print(f"El puerto {PUERTO} esta ocupado: cierra la otra ventana de chat y vuelve a abrir.")
        sys.exit(1)
    print(f"""
  Chat con tu modelo de Kaggle
  ---------------------------
  Abre en el navegador:   http://127.0.0.1:{PUERTO}

  URL en uso:  {UPSTREAM or "(ninguna todavia: pegala en el panel 8081)"}
  TOKEN:       {"(leido del panel)" if TOKEN else "(falta)"}

  Deja esta ventana abierta mientras chateas. Ctrl+C para parar.
""")
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nparado")


if __name__ == "__main__":
    main()
