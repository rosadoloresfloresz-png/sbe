#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
motor-openai-local.py - le sirve a tu panel un LLM de verdad (API), sin tocar el panel.

Por que existe esto
-------------------
Tu panel habla con "motores remotos" por una URL y un TOKEN, y manda el token en la
cabecera X-VS-Token (es el esquema que usa el proxy del notebook de Kaggle). Ninguna API
publica entiende eso, asi que conectarla directo no funciona.

Este programa se pone en medio:

    tu panel  ->  este motor (127.0.0.1:8080, con TOKEN)  ->  API real (DeepSeek, Gemini...)

El panel sigue igual: le pegas http://127.0.0.1:8080 y el TOKEN que sale aqui. Tu clave de
la API vive en config.json, no en el panel. Y cuando quieras cambiar de proveedor, cambias
una linea en config.json y el panel ni se entera.

Ademas traduce el nombre del modelo: el panel pide "razonamiento" y aqui se convierte en el
modelo real del proveedor.

Como se usa
-----------
    python motor-openai-local.py                 # arranca el motor
    python motor-openai-local.py --probar        # comprueba que todo el circuito funciona
    python motor-openai-local.py --config otro.json

La primera vez crea config.json con la plantilla. Pon tu clave ahi (o en la variable de
entorno que diga la plantilla, que es lo mas seguro) y vuelve a ejecutar.

No necesita instalar nada: solo la libreria estandar de Python 3.8+.
"""

import argparse
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.server
import socketserver

AQUI = os.path.dirname(os.path.abspath(__file__))
TOKEN_PLANTILLA = "CAMBIAR-ESTE-TOKEN"

# ---------------------------------------------------------------------------
# Plantilla de configuracion. Los presets son APIs compatibles con OpenAI
# (mismo /v1/chat/completions), que es lo unico que este motor necesita.
# ---------------------------------------------------------------------------
PLANTILLA = {
    "_leeme": [
        "Elige 'proveedor' de la lista de abajo y pon tu clave.",
        "La forma mas segura es dejar la clave en la variable de entorno (campo clave_env)",
        "y no escribirla aqui. Si prefieres pegarla, usa el campo 'clave'.",
        "Genera el TOKEN tu mismo o deja el de plantilla: este motor crea uno y lo guarda.",
        "Aviso: las capas gratuitas suelen tener limites de peticiones y pueden registrar lo",
        "que envias. Si el contenido es sensible, esto no es el camino: usa un modelo local.",
    ],
    "proveedor": "deepseek",
    "token": TOKEN_PLANTILLA,
    "puerto": 8080,
    "modelo_del_panel": "razonamiento",
    "mostrar_cuerpo": False,
    "fingir_comfyui": False,
    "proveedores": {
        "deepseek": {
            "base": "https://api.deepseek.com/v1",
            "modelo": "deepseek-chat",
            "clave_env": "DEEPSEEK_API_KEY",
            "nota": "el mas barato por token; su catalogo de modelos cambia, mira /v1/models",
        },
        "openrouter": {
            "base": "https://openrouter.ai/api/v1",
            "modelo": "deepseek/deepseek-chat-v3.1:free",
            "clave_env": "OPENROUTER_API_KEY",
            "nota": "un solo punto de acceso a muchos modelos; los ':free' no cuestan nada",
        },
        "google": {
            "base": "https://generativelanguage.googleapis.com/v1beta/openai",
            "modelo": "gemini-2.5-flash",
            "clave_env": "GEMINI_API_KEY",
            "nota": "capa gratuita generosa; endpoint compatible con OpenAI",
        },
        "groq": {
            "base": "https://api.groq.com/openai/v1",
            "modelo": "llama-3.3-70b-versatile",
            "clave_env": "GROQ_API_KEY",
            "nota": "modelos abiertos a velocidad muy alta; capa gratuita",
        },
        "cerebras": {
            "base": "https://api.cerebras.ai/v1",
            "modelo": "qwen-3-32b",
            "clave_env": "CEREBRAS_API_KEY",
            "nota": "capa gratuita y de las latencias mas bajas que hay",
        },
        "zhipu": {
            "base": "https://open.bigmodel.cn/api/paas/v4",
            "modelo": "glm-4.5-flash",
            "clave_env": "ZHIPU_API_KEY",
            "nota": "GLM: los modelos 'flash' son gratuitos y razonan bien",
        },
        "mistral": {
            "base": "https://api.mistral.ai/v1",
            "modelo": "magistral-small-latest",
            "clave_env": "MISTRAL_API_KEY",
            "nota": "capa gratuita; magistral es su familia de razonamiento",
        },
        "together": {
            "base": "https://api.together.xyz/v1",
            "modelo": "deepseek-ai/DeepSeek-V3",
            "clave_env": "TOGETHER_API_KEY",
            "nota": "catalogo amplio de modelos abiertos, de pago",
        },
        "local": {
            "base": "http://127.0.0.1:8188/v1",
            "modelo": "razonamiento",
            "clave_env": "",
            "nota": "para probar contra el llama-server del notebook de Kaggle",
        },
    },
}


def cargar_config(ruta):
    if not os.path.exists(ruta):
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(PLANTILLA, f, indent=2, ensure_ascii=False)
        print("No existia %s, te deje una plantilla." % ruta)
        print("Pon tu clave (o la variable de entorno) y vuelve a ejecutar.")
        sys.exit(1)

    with open(ruta, encoding="utf-8") as f:
        cfg = json.load(f)

    if cfg.get("proveedor") not in cfg.get("proveedores", {}):
        print("El campo 'proveedor' (%r) no esta en la lista." % cfg.get("proveedor"))
        print("Opciones:", ", ".join(sorted(cfg.get("proveedores", {}))))
        sys.exit(1)

    # Si el token sigue siendo el de plantilla, se genera uno y se guarda: asi nadie deja
    # el motor abierto con un token conocido.
    if not cfg.get("token") or cfg["token"] == TOKEN_PLANTILLA:
        cfg["token"] = secrets.token_urlsafe(18)
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        print("Token de plantilla detectado: genere uno y lo guarde en %s" % ruta)

    return cfg


def resolver_proveedor(cfg):
    nombre = cfg["proveedor"]
    p = cfg["proveedores"][nombre]
    clave = ""
    if p.get("clave_env"):
        clave = os.environ.get(p["clave_env"], "").strip()
    if not clave:
        clave = (p.get("clave") or "").strip()
    if not clave and nombre != "local":
        print("Falta la clave de %s." % nombre)
        if p.get("clave_env"):
            print("Ponla en la variable de entorno %s o en el campo 'clave' de config.json." % p["clave_env"])
        else:
            print("Ponla en el campo 'clave' de config.json.")
        sys.exit(1)
    return nombre, p["base"].rstrip("/"), p["modelo"], clave


def crear_handler(cfg, base, modelo_real, clave):
    token = cfg["token"]
    modelo_panel = cfg.get("modelo_del_panel", "")
    puerto = cfg.get("puerto", 8080)
    mostrar_cuerpo = bool(cfg.get("mostrar_cuerpo"))
    fingir_comfyui = bool(cfg.get("fingir_comfyui"))
    contador = {"n": 0}

    def log(linea):
        print(linea, flush=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "motor-openai-local"

        def log_message(self, *args):
            pass  # el registro lo llevamos nosotros, con los datos que importan

        # ---------------------------------------------------------------- auth
        def _autorizado(self, query):
            if self.headers.get("X-VS-Token") == token:
                return True, False
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and auth[7:].strip() == token:
                return True, False
            if query.get("token", [""])[0] == token:
                return True, True
            return ("vs_token=" + token) in self.headers.get("Cookie", ""), False

        def _responder(self, codigo, datos, tipo):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(datos)

        def _json(self, codigo, objeto):
            self._responder(codigo, json.dumps(objeto).encode("utf-8"), "application/json")

        def _cookie(self, por_query):
            if por_query:
                self.send_header("Set-Cookie", "vs_token=" + token + "; Path=/; SameSite=Lax")

        # ---------------------------------------------------------------- utilidades
        def _leer_cuerpo(self):
            largo = self.headers.get("Content-Length")
            if largo:
                return self.rfile.read(int(largo))
            return None

        def _traducir_modelo(self, cuerpo):
            """Cambia el nombre de modelo que manda el panel por el real del proveedor.
            Si el cliente pide otro modelo distinto al del panel, se respeta: asi un cliente
            externo (o tu IDE) puede elegir otro modelo sin tocar el panel."""
            if not cuerpo:
                return cuerpo, modelo_real
            try:
                datos = json.loads(cuerpo.decode("utf-8"))
            except Exception:
                return cuerpo, "?"
            if not isinstance(datos, dict):
                return cuerpo, "?"
            pedido = datos.get("model")
            if not pedido or pedido == modelo_panel:
                datos["model"] = modelo_real
                return json.dumps(datos).encode("utf-8"), modelo_real
            # El cliente pidio otro modelo a proposito (un IDE, por ejemplo): se respeta.
            return json.dumps(datos).encode("utf-8"), pedido

        def _destino(self):
            parsed = urllib.parse.urlparse(self.path)
            camino = parsed.path
            if camino.startswith("/v1"):
                camino = camino[3:]
            return base + camino + (("?" + parsed.query) if parsed.query else "")

        # ---------------------------------------------------------------- el proxy
        def _proxy(self):
            t0 = time.time()
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", ""):
                self._json(200, {
                    "motor": "motor-openai-local",
                    "escucha": "127.0.0.1:%d" % puerto,
                    "proveedor": cfg["proveedor"],
                    "modelo": modelo_real,
                    "uso": "/v1/chat/completions con el token en X-VS-Token o Bearer",
                })
                return

            if parsed.path == "/health":
                self._json(200, {"status": "ok"})
                return

            # Algunos paneles validan el motor remoto contra estas rutas de ComfyUI. Se
            # responden solo si 'fingir_comfyui' esta activo, para que la validacion pase.
            if fingir_comfyui and parsed.path == "/system_stats":
                self._json(200, {"system": {"comfyui_version": "motor-openai-local",
                                            "python_version": "3",
                                            "devices": [{"name": cfg["proveedor"], "type": "api"}]}})
                return
            if fingir_comfyui and parsed.path == "/queue":
                self._json(200, {"queue_running": [], "queue_pending": []})
                return

            query = urllib.parse.parse_qs(parsed.query)
            ok, por_query = self._autorizado(query)
            if not ok:
                self._responder(403, b"Token invalido o ausente", "text/plain; charset=utf-8")
                log("[--:--:--] %s %s -> 403 (token)" % (self.command, parsed.path))
                return

            if por_query:
                query.pop("token", None)

            cuerpo = self._leer_cuerpo()
            modelo_visto = "-"
            if cuerpo and parsed.path.endswith("chat/completions"):
                cuerpo, modelo_visto = self._traducir_modelo(cuerpo)

            destino = self._destino()
            # El query se reconstruye sin el token, para no pasarselo al proveedor.
            if por_query and "?" in destino:
                destino = destino.split("?")[0] + (
                    "?" + urllib.parse.urlencode(query, doseq=True) if query else "")

            req = urllib.request.Request(destino, data=cuerpo, method=self.command)
            req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
            req.add_header("Accept", self.headers.get("Accept", "application/json"))
            if clave:
                req.add_header("Authorization", "Bearer " + clave)
            for extra, valor in (cfg["proveedores"][cfg["proveedor"]].get("cabeceras") or {}).items():
                req.add_header(extra, valor)
            # Ojo: NO se reenvian X-VS-Token, Cookie ni Authorization del panel. Tu token no
            # tiene por que llegar al proveedor.

            if mostrar_cuerpo:
                log("    peticion: " + (cuerpo or b"")[:600].decode("utf-8", "replace"))

            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    tipo = r.headers.get("Content-Type", "application/json")

                    if tipo.startswith("text/event-stream") and self.command != "HEAD":
                        # Se reenvia cada linea en cuanto llega. Sin esto, urllib acumularia
                        # toda la respuesta y el panel se quedaria mudo decenas de segundos.
                        self.send_response(r.status)
                        self.send_header("Content-Type", tipo)
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("X-Accel-Buffering", "no")
                        self.send_header("Transfer-Encoding", "chunked")
                        self._cookie(por_query)
                        self.end_headers()
                        bytes_enviados, eventos = 0, 0
                        while True:
                            linea = r.readline()
                            if not linea:
                                break
                            bytes_enviados += len(linea)
                            eventos += 1
                            self.wfile.write(b"%x\r\n" % len(linea) + linea + b"\r\n")
                            self.wfile.flush()
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                        contador["n"] += 1
                        log("[%s] #%d %s modelo=%s stream=si -> %d en %d ms (%d eventos)"
                            % (time.strftime("%H:%M:%S"), contador["n"], parsed.path,
                               modelo_visto, r.status, (time.time() - t0) * 1000, eventos))
                        return

                    datos = r.read()
                    self.send_response(r.status)
                    for k, v in r.headers.items():
                        # Se copian todas menos las de salto a salto; el cuerpo va intacto,
                        # asi que cabeceras como Content-Encoding siguen siendo validas.
                        if k.lower() in ("transfer-encoding", "connection", "content-length",
                                         "keep-alive"):
                            continue
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(datos)))
                    self._cookie(por_query)
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(datos)
                    contador["n"] += 1
                    log("[%s] #%d %s modelo=%s -> %d en %d ms (%d bytes)"
                        % (time.strftime("%H:%M:%S"), contador["n"], parsed.path,
                           modelo_visto, r.status, (time.time() - t0) * 1000, len(datos)))

            except urllib.error.HTTPError as e:
                # El error del proveedor se reenvia tal cual: es la mejor pista que vas a
                # tener (clave invalida, modelo inexistente, cuota agotada...).
                datos = e.read()
                self._responder(e.code, datos, e.headers.get("Content-Type", "application/json"))
                contador["n"] += 1
                log("[%s] #%d %s modelo=%s -> %d (error del proveedor) %s"
                    % (time.strftime("%H:%M:%S"), contador["n"], parsed.path, modelo_visto,
                       e.code, datos[:300].decode("utf-8", "replace")))
            except Exception as e:
                self._responder(502, ("motor: " + str(e)).encode("utf-8"),
                                "text/plain; charset=utf-8")
                log("[%s] %s -> 502 %s: %s" % (time.strftime("%H:%M:%S"), parsed.path,
                                               type(e).__name__, e))

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _proxy

    return Handler


def listar_modelos(base, clave, filtro):
    """Pregunta al proveedor que modelos ofrece de verdad. Es la unica fuente fiable:
    los nombres cambian cada semana y aqui salen los IDs exactos que acepta /v1/chat/completions."""
    req = urllib.request.Request(base + "/models")
    if clave:
        req.add_header("Authorization", "Bearer " + clave)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            datos = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print("El proveedor respondio %d: %s" % (e.code, e.read()[:300].decode("utf-8", "replace")))
        print("Suele ser clave invalida o sin permiso para listar modelos.")
        return 1
    except Exception as e:
        print("No pude consultar la lista:", type(e).__name__, e)
        return 1

    ids = sorted(str(m.get("id")) for m in (datos.get("data") or []))
    if filtro:
        ids = [i for i in ids if filtro.lower() in i.lower()]
    print("%d modelos%s:" % (len(ids), (" que contienen %r" % filtro) if filtro else ""))
    for i in ids:
        print("   ", i)
    if not ids:
        print("    (ninguno; prueba sin filtro para ver el catalogo completo)")
    return 0


# ---------------------------------------------------------------------------
# Prueba de extremo a extremo
# ---------------------------------------------------------------------------
def probar(cfg, puerto, token):
    base_local = "http://127.0.0.1:%d" % puerto
    fallos = 0

    def titulo(t):
        print()
        print("-" * 66)
        print(t)

    def pedir(url, datos=None, cabeceras=None, crudo=False):
        req = urllib.request.Request(url, data=datos, headers=cabeceras or {})
        return urllib.request.urlopen(req, timeout=300)

    titulo("1) Sin token tiene que dar 403 (nadie usa tu clave gratis)")
    try:
        pedir(base_local + "/v1/models")
        print("   *** dio 200: el motor esta abierto, revisa el token")
        fallos += 1
    except urllib.error.HTTPError as e:
        print("   HTTP", e.code, "(correcto)")

    titulo("2) Token por cabecera X-VS-Token (lo que manda tu panel)")
    try:
        with pedir(base_local + "/v1/models", cabeceras={"X-VS-Token": token}) as r:
            datos = json.loads(r.read())
        ids = [m.get("id") for m in (datos.get("data") or [])]
        print("   HTTP", r.status, "| modelos:", ", ".join(str(i) for i in ids[:5]) or "(vacio)")
    except Exception as e:
        print("   *** fallo:", type(e).__name__, e)
        fallos += 1

    titulo("3) Token como Bearer (formato OpenAI, para clientes externos)")
    try:
        with pedir(base_local + "/v1/models",
                   cabeceras={"Authorization": "Bearer " + token}) as r:
            print("   HTTP", r.status, "(correcto)")
    except Exception as e:
        print("   *** fallo:", type(e).__name__, e)
        fallos += 1

    titulo("4) Respuesta normal: el panel pide '%s' y debe llegar al modelo real"
           % cfg.get("modelo_del_panel"))
    cuerpo = json.dumps({
        "model": cfg.get("modelo_del_panel"),
        "max_tokens": 200,
        "messages": [{"role": "user", "content": "Responde solo con la palabra FUNCIONA."}],
    }).encode("utf-8")
    cab = {"X-VS-Token": token, "Content-Type": "application/json"}
    try:
        t0 = time.time()
        with pedir(base_local + "/v1/chat/completions", cuerpo, cab) as r:
            datos = json.loads(r.read())
        msj = datos["choices"][0]["message"]
        print("   modelo devuelto:", datos.get("model"))
        print("   texto:", (msj.get("content") or "").strip()[:200])
        if msj.get("reasoning_content"):
            print("   razonamiento separado: %d caracteres" % len(msj["reasoning_content"]))
        print("   tiempo: %.1f s" % (time.time() - t0))
    except Exception as e:
        print("   *** fallo:", type(e).__name__, e)
        fallos += 1

    titulo("5) Streaming: la primera palabra tiene que llegar ANTES del final")
    try:
        import json as _json
        cuerpo = _json.dumps({
            "model": cfg.get("modelo_del_panel"),
            "stream": True,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": "Cuenta del 1 al 30 separado por comas."}],
        }).encode("utf-8")
        t0 = time.time()
        primero, eventos, ultimo = None, 0, 0
        with pedir(base_local + "/v1/chat/completions", cuerpo, cab) as r:
            for linea in r:
                if linea.startswith(b"data: "):
                    eventos += 1
                    if primero is None:
                        primero = time.time() - t0
                    ultimo = time.time() - t0
        print("   %d eventos | primero a %.2f s | ultimo a %.2f s" % (eventos, primero or 0, ultimo))
        if eventos <= 1:
            print("   *** muy pocos eventos: el streaming no esta pasando")
            fallos += 1
        elif primero is not None and primero > max(2.0, ultimo * 0.9):
            print("   *** el primero llego casi al final: se esta acumulando la respuesta")
            fallos += 1
        else:
            print("   (el streaming llega trozo a trozo, correcto)")
    except Exception as e:
        print("   *** fallo:", type(e).__name__, e)
        fallos += 1

    print()
    print("=" * 66)
    if fallos:
        print("Fallaron %d comprobaciones." % fallos)
        print("Si el 4 o el 5 fallan con 401/404, el problema es del proveedor:")
        print("clave invalida, modelo inexistente o cuota agotada. El mensaje de error")
        print("que se reenvia es el del proveedor, leelo.")
    else:
        print("Todo correcto. En el panel:")
        print("   URL    : http://127.0.0.1:%d" % puerto)
        print("   TOKEN  : %s" % token)
        print("   Modelo : %s" % cfg.get("modelo_del_panel"))
    print("=" * 66)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Motor LLM compatible-OpenAI para tu panel.")
    ap.add_argument("--config", default=os.path.join(AQUI, "config.json"))
    ap.add_argument("--probar", action="store_true",
                    help="comprueba el circuito completo y sale")
    ap.add_argument("--modelos", nargs="?", const="", default=None,
                    help="lista los modelos que ofrece el proveedor; acepta un filtro: "
                         "--modelos mimo")
    args = ap.parse_args()

    cfg = cargar_config(args.config)
    nombre, base, modelo_real, clave = resolver_proveedor(cfg)
    puerto = int(cfg.get("puerto", 8080))

    if args.modelos is not None:
        print("Proveedor: %s (%s)" % (nombre, base))
        sys.exit(listar_modelos(base, clave, args.modelos))

    if args.probar:
        probar(cfg, puerto, cfg["token"])
        return

    print("Proveedor : %s (%s)" % (nombre, base))
    print("Modelo    : %s  <-- el panel lo pide como %r"
          % (modelo_real, cfg.get("modelo_del_panel")))
    print("Clave     : %s" % ("configurada" if clave else "ninguna (proveedor local)"))
    print("Escuchando: http://127.0.0.1:%d" % puerto)
    print()
    print("En el panel -> Motores -> Motor remoto:")
    print("    URL    : http://127.0.0.1:%d" % puerto)
    print("    TOKEN  : %s" % cfg["token"])
    print()
    print("Deja esta ventana abierta. Ctrl+C para parar.")

    Handler = crear_handler(cfg, base, modelo_real, clave)

    class Servidor(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        servidor = Servidor(("127.0.0.1", puerto), Handler)
    except OSError as e:
        print()
        print("No pude escuchar en el puerto %d: %s" % (puerto, e))
        print("Suele ser que ya hay otro motor corriendo. Cierra el anterior o cambia")
        print("'puerto' en config.json.")
        sys.exit(1)

    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    try:
        while hilo.is_alive():
            hilo.join(1)
    except KeyboardInterrupt:
        print()
        print("Parando.")
        servidor.shutdown()


if __name__ == "__main__":
    main()
