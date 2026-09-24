#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
zcode-anthropic-proxy.py - habla el protocolo de Anthropic hacia ZCode y OpenAI hacia tu tunel.

Por que hace falta
------------------
ZCode pide los modelos por la API de Anthropic (POST /v1/messages). llama.cpp solo entiende
la API de OpenAI (/v1/chat/completions). Este programa traduce en los dos sentidos:

    ZCode  --(Anthropic /v1/messages)-->  este proxy  --(OpenAI /v1/chat/completions)-->  tunel

Traduce mensajes, bloques de contenido, herramientas (tools), razonamiento y streaming SSE,
para que el modelo aparezca en ZCode como un proveedor mas.

Dos avisos que ahorran tiempo:

- Las tools SOLO funcionan si llama-server arranco con --jinja (nivel 1 o 2 de la seccion 4
  del notebook). Sin el, cualquier peticion con tools devuelve error.
- llama-server reciente ya trae /v1/messages nativo, asi que este proxy se puede saltar.
  Sigue siendo util por dos cosas: recorta el historial cuando la sesion de ZCode no cabe
  en el contexto del modelo (32k) y deja en el registro que tools se llamaron.

Uso
---
    python zcode-anthropic-proxy.py --upstream https://algo.trycloudflare.com --token ABC123

    --upstream   URL del tunel (la seccion 7 del notebook la imprime)
    --token      TOKEN del tunel
    --puerto     puerto local (por defecto 8081)

Cuando la sesion de Kaggle cambie (URL y TOKEN nuevos), no hace falta reiniciar ni tocar
ningun archivo: abre el panel

    http://127.0.0.1:8081/panel

y pega ahi el bloque de la seccion 7 (URL y TOKEN). El proxy lo guarda, lo comprueba y sigue
con la URL nueva. Tambien lo relee solo si editas zcode-anthropic-proxy.json a mano.

En ZCode: proveedor con baseURL http://127.0.0.1:8081/v1 y el modelo 'razonamiento'.
Solo libreria estandar. Deja esta ventana abierta mientras uses el modelo.
"""

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import http.server
import socketserver

# Con la salida redirigida a un archivo (o a Tee-Object), Python la bufferiza por bloques y el
# registro no aparece hasta juntar 8 KB: parece que el proxy no hace nada. Se fuerza linea a linea.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# Este programa solo habla con el tunel de Cloudflare. Si el sistema tiene variables de proxy
# (HTTPS_PROXY / ALL_PROXY) apuntando a algo que ya no esta corriendo (Psiphon, Tor...), urllib
# intentaria pasar por ahi y cada peticion al tunel fallaria con URLError a los dos segundos:
# eso se confunde con "el cliente corto la conexion" y no hay forma de adivinarlo desde fuera.
# Por eso aqui se ignora el proxy del sistema a proposito.
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

# Donde se recuerdan la URL y el TOKEN de la ultima vez, para no repetirlos.
GUARDADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zcode-anthropic-proxy.json")


# ---------------------------------------------------------------------------
# Estado: la URL y el TOKEN en uso, que se pueden cambiar sin reiniciar
# ---------------------------------------------------------------------------

def extraer_datos(texto):
    """Saca la URL y el TOKEN de lo que pegues.

    Vale el bloque de la seccion 7 del notebook ('URL            : https://...'),
    el CONEXION.txt ('URL=... TOKEN=...') o cualquier texto donde aparezcan.
    Devuelve (url, token); cualquiera de los dos puede ser None."""
    texto = texto or ""
    url = None
    m = re.search(r"https?://[^\s\"'<>|]+", texto)
    if m:
        url = m.group(0).rstrip(".,;)")
    else:
        # Sin el https:// delante, pero con el dominio del tunel: vale igual.
        m = re.search(r"\b([a-z0-9][a-z0-9\-]*\.trycloudflare\.com)\b", texto, re.IGNORECASE)
        if m:
            url = "https://" + m.group(1)
    token = None
    for patron in (r"TOKEN\s*[:=]\s*([A-Za-z0-9_\-\.]{6,})",
                   r"Bearer\s+([A-Za-z0-9_\-\.]{6,})",
                   r"[?&]token=([A-Za-z0-9_\-\.]{6,})",
                   r"(?:api[_ ]?key|token)\s*[:=]?\s*([A-Za-z0-9_\-\.]{6,})"):
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            token = m.group(1)
            break
    return (limpiar_url(url) if url else None), token


def token_corto(token):
    """Para poder ensenar el TOKEN sin ensenarlo entero."""
    if not token:
        return "(ninguno)"
    return token[:6] + "..." + token[-4:] if len(token) > 12 else token[:3] + "..."


class Estado:
    """URL/TOKEN en uso. Se relee el JSON si cambia y se actualiza desde el panel."""

    def __init__(self, upstream, token, modelo, limite_tokens):
        self.upstream = upstream
        self.token = token
        self.modelo = modelo
        self.limite_tokens = limite_tokens
        self.mtime = self._mtime()
        self.ultima = None          # (hora, ok, detalle) de la ultima comprobacion
        self.candado = threading.Lock()

    # -- persistencia ------------------------------------------------------
    def _mtime(self):
        try:
            return os.path.getmtime(GUARDADO)
        except OSError:
            return None

    def guardar(self):
        try:
            with open(GUARDADO, "w", encoding="utf-8") as f:
                json.dump({"upstream": self.upstream, "token": self.token,
                           "modelo": self.modelo}, f, indent=2)
            self.mtime = self._mtime()
        except Exception as e:
            print("[%s] no pude guardar %s: %s"
                  % (time.strftime("%H:%M:%S"), os.path.basename(GUARDADO), e))

    def adoptar(self, upstream, token=None):
        """Cambia el destino en caliente (panel, o el JSON editado a mano)."""
        with self.candado:
            cambiado = upstream != self.upstream or (token and token != self.token)
            self.upstream = upstream
            if token:
                self.token = token
            return cambiado

    def refrescar(self):
        """Si el JSON cambio en disco, se adopta sin reiniciar el proxy."""
        m = self._mtime()
        if m is None or m == self.mtime:
            return
        self.mtime = m
        try:
            with open(GUARDADO, encoding="utf-8") as f:
                datos = json.load(f)
        except Exception:
            return
        arriba = datos.get("upstream")
        if arriba and self.adoptar(limpiar_url(arriba) or arriba, datos.get("token")):
            print("[%s] destino actualizado desde %s: %s"
                  % (time.strftime("%H:%M:%S"), os.path.basename(GUARDADO), self.upstream))

    # -- comprobacion ------------------------------------------------------
    def comprobar(self):
        # Se guarda TAMBIEN contra que destino se hizo: si luego cambia la URL, ese "ok"
        # ya no vale y no se puede ensenar como si el tunel de ahora estuviera vivo.
        arriba = self.upstream
        ok, detalle = comprobar(arriba, self.token)
        self.ultima = (time.strftime("%H:%M:%S"), ok, detalle, arriba)
        return ok, detalle

    def anotar_fallo(self, detalle):
        """Deja constancia de un fallo al hablar con el tunel, desde una peticion de ZCode.

        Tiene que guardar los MISMOS cuatro campos que comprobar(): si no, el panel y
        /health revientan al leerlo (y el panel deja de cargar justo cuando mas falta hace).
        """
        self.ultima = (time.strftime("%H:%M:%S"), False, detalle, self.upstream)

    # -- contexto del servidor --------------------------------------------
    def contexto(self):
        """Pregunta al servidor su contexto real (n_ctx), para recortar lo justo.

        Es lo que hace que el mismo proxy valga con -c 32768 o con -c 131072: si el notebook
        se arranca con mas contexto, aqui se aprovecha solo. Devuelve None si no se puede."""
        for ruta in ("/props", "/v1/props"):
            try:
                req = urllib.request.Request(self.upstream.rstrip("/") + ruta,
                                             headers={"Authorization": "Bearer " + self.token})
                with urllib.request.urlopen(req, timeout=30) as r:
                    datos = json.loads(r.read())
                ajustes = datos.get("default_generation_settings") or {}
                n = int(ajustes.get("n_ctx") or datos.get("n_ctx") or 0)
                if n > 0:
                    return n
            except Exception:
                continue
        return None


def ultima_comprobacion(estado):
    """(hora, ok, detalle, upstream) de la ultima comprobacion, o None.

    Tolerante con el formato: si algun dia se guarda con menos campos, se completa con el
    destino actual en vez de reventar con ValueError."""
    datos = estado.ultima
    if not datos:
        return None
    if len(datos) == 3:
        return datos[0], datos[1], datos[2], estado.upstream
    return tuple(datos[:4])


def estado_salud(estado, puerto):
    salud = {"status": "ok", "upstream": estado.upstream, "modelo": estado.modelo,
             "panel": "http://127.0.0.1:%d/panel" % puerto, "token": token_corto(estado.token)}
    ultima = ultima_comprobacion(estado)
    if ultima:
        hora, ok, detalle, arriba = ultima
        if arriba != estado.upstream:
            # La comprobacion era de otro destino: no vale como estado del actual.
            ok, detalle = False, "sin comprobar con el destino actual (la ultima prueba fue de %s)" % arriba
        salud["tunel"] = {"ok": ok, "detalle": detalle, "hora": hora,
                          "upstream_comprobado": arriba,
                          "vigente": arriba == estado.upstream}
    return salud


def panel_html(estado, puerto, resultado=None, aviso=None, pegado=""):
    """El panel: pegas ahi la seccion 7 del notebook y el proxy cambia de tunel solo."""
    ultima = ultima_comprobacion(estado)
    if resultado:
        ok, detalle = resultado
        caja = ('<p class="%s"><b>%s</b> %s</p>'
                % ("ok" if ok else "mal", "El tunel responde." if ok else "El tunel NO responde:",
                   html.escape(detalle)))
    elif ultima:
        hora, ok, detalle, arriba = ultima
        caduco = "" if arriba == estado.upstream else " (ojo: esa prueba era de %s, ya no es el destino actual)" % arriba
        caja = ('<p class="%s">Ultima comprobacion (%s): %s%s</p>'
                % ("ok" if ok and not caduco else "mal", hora, html.escape(detalle), html.escape(caduco)))
    else:
        caja = '<p class="aviso">Todavia no se ha comprobado el tunel.</p>'
    if aviso:
        caja = '<p class="ok">%s</p>' % html.escape(aviso) + caja
    return """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>proxy zcode - tunel de Kaggle</title>
<style>
 body {{ background:#14161a; color:#e8e8e8; font:15px/1.5 system-ui,Segoe UI,sans-serif;
        margin:0; padding:28px; }}
 h1 {{ font-size:20px; margin:0 0 4px; }}
 p  {{ margin:10px 0; }}
 code {{ background:#22252c; padding:2px 6px; border-radius:4px; }}
 textarea {{ width:100%; min-height:150px; background:#0e1013; color:#e8e8e8; border:1px solid #333;
             border-radius:6px; padding:10px; font:13px/1.45 Consolas,monospace; }}
 input[type=text] {{ width:100%; background:#0e1013; color:#e8e8e8; border:1px solid #333;
                     border-radius:6px; padding:8px; font:13px Consolas,monospace; }}
 button {{ background:#2f6fed; color:#fff; border:0; border-radius:6px; padding:10px 16px;
           font-size:15px; cursor:pointer; }}
 button.sec {{ background:#3a3f47; }}
 .ok  {{ color:#7ee08a; }}
 .mal {{ color:#ff8b8b; }}
 .aviso {{ color:#e8c877; }}
 .caja {{ background:#1b1e24; border:1px solid #2a2e36; border-radius:8px; padding:14px 16px;
          margin:16px 0; }}
 table {{ border-collapse:collapse; }}
 td {{ padding:3px 14px 3px 0; }}
 .pie {{ color:#8b93a1; font-size:13px; margin-top:22px; }}
</style></head><body>
<h1>Proxy Anthropic &rarr; Kaggle</h1>
<p>ZCode habla con <code>http://127.0.0.1:{puerto}/v1</code>. Lo unico que cambia en cada
sesion de Kaggle es el tunel; pegalo aqui y el proxy se actualiza solo.</p>
<div class="caja">
 <table>
  <tr><td>Destino</td><td><code>{upstream}</code></td></tr>
  <tr><td>TOKEN</td><td><code>{token}</code></td></tr>
  <tr><td>Modelo</td><td><code>{modelo}</code></td></tr>
 </table>
</div>
{caja}
<form method="post" action="/panel">
 <p><b>Pega aqui la seccion 7 del notebook</b> (las lineas URL y TOKEN):</p>
 <textarea name="datos" placeholder="URL            : https://algo.trycloudflare.com
TOKEN          : xxxxxxxxxxxxxxxxxxxxxxxxx">{pegado}</textarea>
 <p>O solo el TOKEN, si la URL no ha cambiado:<br>
 <input type="text" name="token" placeholder="(opcional) TOKEN nuevo"></p>
 <p><button type="submit">Guardar y comprobar</button></p>
</form>
<form method="get" action="/panel">
 <p><button class="sec" type="submit" name="comprobar" value="1">Solo comprobar el tunel</button></p>
</form>
<p class="pie">Si el tunel no responde: en Kaggle, seccion 7 (la del tunel) y mira que la
sesion siga viva. La URL y el TOKEN cambian con cada sesion nueva.</p>
</body></html>""".format(puerto=puerto, upstream=html.escape(estado.upstream),
                        token=html.escape(token_corto(estado.token)),
                        modelo=html.escape(estado.modelo), caja=caja,
                        pegado=html.escape(pegado))


# ---------------------------------------------------------------------------
# Traduccion de la peticion: Anthropic Messages -> OpenAI Chat Completions
# ---------------------------------------------------------------------------

def texto_de_bloques(contenido):
    """Acepta tanto una cadena como la lista de bloques de Anthropic y devuelve texto."""
    if isinstance(contenido, str):
        return contenido
    trozos = []
    for b in contenido or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            trozos.append(b.get("text") or "")
        elif b.get("type") == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                trozos.append(texto_de_bloques(c))
            else:
                trozos.append(str(c or ""))
    return "\n".join(t for t in trozos if t)


def herramientas_a_openai(tools):
    """Acepta herramientas en formato Anthropic y tambien en formato OpenAI.

    ZCode manda el de Anthropic (name/description/input_schema), pero si un cliente manda ya
    el de OpenAI ({"type":"function","function":{...}}) hay que leer de ahi description y
    parameters: si no, las herramientas llegan al modelo sin esquema y sin descripcion (y el
    modelo no sabe usarlas)."""
    if not tools:
        return None
    salida = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else {}
        nombre = t.get("name") or fn.get("name")
        if not nombre:
            continue
        parametros = (t.get("input_schema") or t.get("parameters")
                      or fn.get("parameters") or {"type": "object", "properties": {}})
        salida.append({
            "type": "function",
            "function": {
                "name": nombre,
                "description": t.get("description") or fn.get("description") or "",
                "parameters": parametros,
            },
        })
    return salida or None


def peticion_a_openai(cuerpo):
    """Convierte el cuerpo de /v1/messages al de /v1/chat/completions."""
    mensajes = []

    sistema = cuerpo.get("system")
    if sistema:
        mensajes.append({"role": "system", "content": texto_de_bloques(sistema)})

    for m in cuerpo.get("messages") or []:
        rol = m.get("role")
        contenido = m.get("content")

        if isinstance(contenido, str):
            mensajes.append({"role": rol, "content": contenido})
            continue

        textos, llamadas, resultados = [], [], []
        for b in contenido or []:
            if not isinstance(b, dict):
                continue
            tipo = b.get("type")
            if tipo == "text":
                textos.append(b.get("text") or "")
            elif tipo == "tool_use":
                llamadas.append({
                    "id": b.get("id") or ("call_" + uuid.uuid4().hex[:20]),
                    "type": "function",
                    "function": {"name": b.get("name") or "",
                                 "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)},
                })
            elif tipo == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = texto_de_bloques(c)
                resultados.append({"role": "tool",
                                   "tool_call_id": b.get("tool_use_id") or "",
                                   "content": str(c if c is not None else "")})
            # Los bloques de tipo "thinking" del historial se descartan: el modelo no los
            # necesita de vuelta y reenviarlos confunde a llama-server.

        if textos or llamadas:
            msg = {"role": rol, "content": "\n".join(t for t in textos if t) or ""}
            if llamadas:
                msg["tool_calls"] = llamadas
                if not msg["content"]:
                    msg["content"] = None
            mensajes.append(msg)
        mensajes.extend(resultados)

    salida = {
        "model": cuerpo.get("model") or "razonamiento",
        "messages": mensajes,
        "stream": bool(cuerpo.get("stream")),
    }
    if cuerpo.get("max_tokens"):
        salida["max_tokens"] = int(cuerpo["max_tokens"])
    if cuerpo.get("temperature") is not None:
        salida["temperature"] = cuerpo["temperature"]
    if cuerpo.get("top_p") is not None:
        salida["top_p"] = cuerpo["top_p"]
    if cuerpo.get("stop_sequences"):
        salida["stop"] = cuerpo["stop_sequences"]

    tools = herramientas_a_openai(cuerpo.get("tools"))
    if tools:
        salida["tools"] = tools
        # tool_choice en llama-server es SOLO una cadena: "auto", "none" o "required".
        # El formato objeto de OpenAI ({"type":"function",...}) lo rechaza con un error de
        # tipo, asi que "forzar una tool concreta" se traduce a "required", que es lo mas
        # cerca que se puede quedar. La propia conversion Anthropic->OpenAI de llama.cpp
        # hace exactamente este mapeo.
        eleccion = cuerpo.get("tool_choice")
        if isinstance(eleccion, dict):
            tipo = eleccion.get("type")
            if tipo in ("tool", "any"):
                salida["tool_choice"] = "required"
            elif tipo == "auto":
                salida["tool_choice"] = "auto"
            elif tipo == "none":
                salida["tool_choice"] = "none"
        elif isinstance(eleccion, str) and eleccion in ("auto", "none", "required"):
            salida["tool_choice"] = eleccion
    return salida


# ---------------------------------------------------------------------------
# Traduccion de la respuesta: OpenAI -> Anthropic
# ---------------------------------------------------------------------------

def parar_motivo(finish):
    if finish == "tool_calls":
        return "tool_use"
    if finish == "length":
        return "max_tokens"
    return "end_turn"


def respuesta_a_anthropic(datos, modelo_pedido):
    msj = ((datos.get("choices") or [{}])[0].get("message")) or {}
    bloques = []

    pensamiento = msj.get("reasoning_content") or ""
    if pensamiento:
        bloques.append({"type": "thinking", "thinking": pensamiento, "signature": ""})

    if msj.get("content"):
        bloques.append({"type": "text", "text": msj["content"]})

    for llamada in msj.get("tool_calls") or []:
        fn = llamada.get("function") or {}
        try:
            entrada = json.loads(fn.get("arguments") or "{}")
        except Exception:
            entrada = {}
        bloques.append({"type": "tool_use", "id": llamada.get("id") or "toolu_" + uuid.uuid4().hex[:16],
                        "name": fn.get("name") or "", "input": entrada})

    if not bloques:
        bloques.append({"type": "text", "text": ""})

    uso = datos.get("usage") or {}
    fin = ((datos.get("choices") or [{}])[0]).get("finish_reason")
    return {
        "id": "msg_" + uuid.uuid4().hex[:20],
        "type": "message",
        "role": "assistant",
        "model": modelo_pedido,
        "content": bloques,
        "stop_reason": parar_motivo(fin),
        "stop_sequence": None,
        "usage": {"input_tokens": uso.get("prompt_tokens", 0),
                  "output_tokens": uso.get("completion_tokens", 0)},
    }


# Cuantos caracteres por token se suponen al estimar. Medido en una peticion real: 30712
# estimados con 4 y 35495 reales (el JSON de las herramientas se tokeniza peor que la prosa).
# Con 3 se estima de mas, que es el lado bueno para equivocarse: mejor recortar de mas que
# comerse un 400 del servidor por pasarse del contexto.
CAR_POR_TOKEN = 3


def reserva_efectiva(limite_tokens, pedida):
    """Cuantos tokens de salida se reservan de verdad.

    ZCode pide max_tokens=32000: con un contexto de 32k eso dejaba el prompt con un suelo de
    500 tokens y el recorte tiraba hasta el system. Se reserva como mucho un tercio del limite
    (con suelo de 1024), que es lo que el servidor puede dar sin comerse el prompt."""
    return min(pedida, max(1024, limite_tokens // 3))


def recortar(mensajes, limite_tokens, reserva_salida):
    """Ajusta la conversacion al limite SIN dejarla nunca sin pregunta.

    ZCode manda la sesion entera y ademas decenas de herramientas: en el caso real eran 53
    herramientas (~26.000 tokens) de un contexto de 32k. El recorte viejo tiraba mensajes por
    delante hasta caber y, con ese gasto, borraba tambien el mensaje del usuario: al servidor
    le llegaba solo el system prompt y contestaba 500 ("No user query found in messages."), que
    es lo que se veia como "no responde".

    Ahora se hace al reves: se conserva lo mas nuevo (la pregunta actual, intacta si cabe) y, si
    falta sitio, se recorta el system prompt o la propia pregunta antes que borrarlos. La
    estimacion es caracteres/3 (con /4 se quedaba corta y el servidor devolvia 400 por pasarse
    del contexto).

    Tres cosas no se caen nunca, porque su falta es justo la que da el 500 en bucle:
      - el ultimo mensaje: cuando ZCode acaba de ejecutar herramientas es un "tool" y es lo que
        el modelo tiene que atender ahora,
      - la ultima pregunta del usuario: sin un mensaje de usuario con texto, la plantilla de
        Qwen lanza "No user query found in messages.",
      - el system prompt: se recorta, no se tira (ahi van las reglas de ZCode)."""
    def tam(m):
        return len(json.dumps(m, ensure_ascii=False)) // CAR_POR_TOKEN

    def recortar_texto(m, tope):
        """Corta el texto del mensaje para que quepa en 'tope' tokens (aviso y JSON incluidos)."""
        c = m.get("content")
        if not isinstance(c, str) or tope <= 0:
            return m
        aviso = " [...recortado: la sesion no cabe en el contexto]"
        limite_car = max(200, tope * CAR_POR_TOKEN - len(aviso) - 64)
        if len(c) <= limite_car:
            return m
        nuevo = dict(m)
        nuevo["content"] = c[:limite_car] + aviso
        return nuevo

    def es_pregunta(m):
        c = m.get("content")
        return m.get("role") == "user" and isinstance(c, str) and bool(c.strip())

    total = sum(tam(m) for m in mensajes)
    if total + reserva_salida <= limite_tokens:
        return mensajes, 0, total

    sistema = [m for m in mensajes if m.get("role") == "system"]
    resto = [m for m in mensajes if m.get("role") != "system"]
    if not resto:
        return mensajes, 0, total

    presupuesto = max(600, limite_tokens - reserva_salida)

    # La ultima pregunta de la sesion, buscada antes de recortar: es intocable.
    pregunta = None
    for m in reversed(resto):
        if es_pregunta(m):
            pregunta = m
            break
    pregunta_es_ultimo = pregunta is not None and pregunta is resto[-1]

    # 1) El ultimo mensaje entra siempre (recortado si el solo ya no cabe).
    ultimo = resto[-1]
    if tam(ultimo) > max(200, presupuesto // 2):
        ultimo = recortar_texto(ultimo, max(200, presupuesto // 2))
    presupuesto -= tam(ultimo)

    # 2) El system prompt se recorta antes que tirarlo (antes se tiraba entero).
    sis = []
    for m in sistema:
        if tam(m) <= presupuesto:
            sis.append(m)
            presupuesto -= tam(m)
        else:
            m2 = recortar_texto(m, presupuesto)
            if m2 is not m and tam(m2) <= presupuesto:
                sis.append(m2)
                presupuesto -= tam(m2)

    # 3) Y despues los mensajes anteriores, del mas nuevo al mas viejo, mientras quepan.
    cola = []
    for m in reversed(resto[:-1]):
        if tam(m) <= presupuesto:
            cola.append(m)
            presupuesto -= tam(m)
        else:
            break
    cola.reverse()
    # Un mensaje "tool" suelto al principio no tiene sentido sin su llamada: se descarta.
    while cola and cola[0].get("role") == "tool":
        cola.pop(0)

    # 4) Si en lo conservado no quedo ninguna pregunta, se mete la ultima (recortada) delante:
    #    sin ella la plantilla del modelo devuelve 500 y ZCode reintenta en bucle.
    if not pregunta_es_ultimo and not any(es_pregunta(m) for m in cola):
        q = pregunta
        if q is not None:
            if tam(q) > presupuesto:
                q = recortar_texto(q, max(160, presupuesto))
            while cola and tam(q) > presupuesto:   # se le hace sitio quitando lo mas viejo
                presupuesto += tam(cola.pop(0))
            while cola and cola[0].get("role") == "tool":
                cola.pop(0)
            cola = [q] + cola

    final = sis + cola + [ultimo]
    return final, len(mensajes) - len(final), limite_tokens - presupuesto


# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------

def crear_handler(estado, puerto):
    def log(texto):
        print(texto, flush=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "zcode-anthropic-proxy"

        def log_message(self, *a):
            pass

        def setup(self):
            # Se anota si ya se empezo a contestar: si el tunel corta antes de eso, todavia
            # se le puede mandar un aviso al cliente en vez de dejarlo con la conexion cerrada.
            super().setup()
            self.ya_respondi = False

        def send_response(self, *a, **k):
            self.ya_respondi = True
            return http.server.BaseHTTPRequestHandler.send_response(self, *a, **k)

        def _json(self, codigo, obj):
            datos = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(codigo)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)
            except OSError:
                pass  # el cliente ya se fue: no hay a quien contestar

        def _html(self, codigo, texto):
            datos = texto.encode("utf-8")
            try:
                self.send_response(codigo)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)
            except OSError:
                pass

        def _error(self, codigo, mensaje, tipo="api_error"):
            self._json(codigo, {"type": "error", "error": {"type": tipo, "message": mensaje}})

        def _sse_cabeceras(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

        def _evento(self, tipo, obj):
            # Anthropic manda el nombre del evento y luego el data como JSON.
            cuerpo = ("event: %s\ndata: %s\n\n" % (tipo, json.dumps(obj, ensure_ascii=False))).encode("utf-8")
            self.wfile.write(b"%x\r\n" % len(cuerpo) + cuerpo + b"\r\n")
            self.wfile.flush()

        def _fin_chunked(self):
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def do_GET(self):
            estado.refrescar()
            parsed = urllib.parse.urlparse(self.path)
            camino = parsed.path
            if camino.endswith("/models"):
                self._json(200, {"data": [{"id": estado.modelo, "type": "model",
                                           "display_name": "Kaggle T4 (razonamiento)"}]})
            elif camino.rstrip("/") in ("", "/health"):
                self._json(200, estado_salud(estado, puerto))
            elif camino.rstrip("/") == "/panel":
                resultado = None
                if urllib.parse.parse_qs(parsed.query).get("comprobar"):
                    log("[%s] comprobacion pedida desde el panel" % time.strftime("%H:%M:%S"))
                    resultado = estado.comprobar()
                self._html(200, panel_html(estado, puerto, resultado))
            else:
                self._error(404, "ruta no soportada: " + camino, "not_found_error")

        def _panel(self):
            """El formulario del panel: guarda la URL/TOKEN nuevos y los comprueba."""
            largo = int(self.headers.get("Content-Length") or 0)
            crudo = self.rfile.read(largo).decode("utf-8", "replace")
            campos = urllib.parse.parse_qs(crudo)
            pegado = (campos.get("datos") or [""])[0]
            aparte = (campos.get("token") or [""])[0].strip()
            url, token = extraer_datos(pegado)
            if not token:
                token = aparte or None
            if not url:
                self._html(400, panel_html(estado, puerto,
                                           (False, "No encontre ninguna URL https://... en el texto."),
                                           pegado=pegado))
                return
            viejo_url, viejo_token = estado.upstream, estado.token
            estado.adoptar(url, token)
            estado.guardar()
            cambios = []
            if estado.upstream != viejo_url:
                cambios.append("destino: %s -> %s" % (viejo_url, estado.upstream))
            if estado.token != viejo_token:
                cambios.append("TOKEN nuevo (%s)" % token_corto(estado.token))
            log("[%s] panel: %s" % (time.strftime("%H:%M:%S"), "; ".join(cambios) or "sin cambios"))
            ok, detalle = estado.comprobar()
            aviso = ("Cambios: " + "; ".join(cambios)) if cambios \
                else "Nada que cambiar: los datos pegados son los que ya estaban."
            self._html(200, panel_html(estado, puerto, (ok, detalle), aviso=aviso))

        def do_POST(self):
            camino = urllib.parse.urlparse(self.path).path
            if camino.rstrip("/") == "/panel":
                self._panel()
                return

            estado.refrescar()
            if not camino.endswith("/messages"):
                self._error(404, "este proxy solo atiende /v1/messages", "not_found_error")
                return

            largo = int(self.headers.get("Content-Length") or 0)
            try:
                cuerpo = json.loads(self.rfile.read(largo) or b"{}")
            except Exception as e:
                self._error(400, "cuerpo invalido: %s" % e, "invalid_request_error")
                return

            modelo_pedido = cuerpo.get("model") or estado.modelo
            pedido = peticion_a_openai(cuerpo)
            pedido["model"] = estado.modelo

            # La sesion entera de ZCode no cabe en un modelo de 32k: se recorta el historial
            # viejo en vez de dejar que el servidor devuelva un 400. Ojo: las herramientas
            # (ZCode manda decenas) tambien ocupan prompt, asi que se descuentan del tope.
            herramientas = pedido.get("tools") or []
            gasto_tools = len(json.dumps(herramientas, ensure_ascii=False)) // CAR_POR_TOKEN \
                if herramientas else 0
            if gasto_tools > estado.limite_tokens - 1000:
                log("[%s] aviso: las herramientas solas ocupan ~%d de los %d tokens; el prompt "
                    "va al minimo" % (time.strftime("%H:%M:%S"), gasto_tools, estado.limite_tokens))
            limite = max(2000, estado.limite_tokens - gasto_tools)
            # ZCode pide hasta 32000 tokens de salida: con 53 herramientas dentro de un contexto
            # de 32k esa reserva se comia el prompt entero (hasta el system). Se ajusta a lo que
            # de verdad cabe y se le manda al servidor el mismo tope.
            reserva = reserva_efectiva(limite, pedido.get("max_tokens") or 4096)
            pedido["max_tokens"] = reserva
            pedido["messages"], quitados, aprox = recortar(pedido["messages"], limite, reserva)

            t0 = time.time()
            aviso = (" | RECORTE: fuera %d mensajes viejos" % quitados) if quitados else ""
            # Resumen de lo que de verdad se le manda al modelo: si el mensaje del usuario se
            # cayo en el recorte, aqui se ve (es la causa tipica de "responde cualquier cosa").
            resumen = " ".join("%s:%d" % (m.get("role"), len(json.dumps(m.get("content") or "",
                                                         ensure_ascii=False)) // 4)
                               for m in pedido["messages"][:5])
            log("[%s] -> %s | stream=%s | %d mensajes | %d tools | ~%d tokens%s"
                % (time.strftime("%H:%M:%S"), camino, pedido["stream"],
                   len(pedido["messages"]), len(herramientas), aprox + gasto_tools, aviso))
            log("            msgs[%s]" % resumen)

            req = urllib.request.Request(
                estado.upstream.rstrip("/") + "/chat/completions",
                data=json.dumps(pedido, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Accept": "text/event-stream" if pedido["stream"] else "application/json",
                         "Authorization": "Bearer " + estado.token},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    if not pedido["stream"]:
                        datos = json.loads(r.read())
                        salida = respuesta_a_anthropic(datos, modelo_pedido)
                        nombres = [b.get("name") for b in salida["content"]
                                   if b.get("type") == "tool_use" and b.get("name")]
                        log("[%s] <- %s | %d bloques | %.1f s%s"
                            % (time.strftime("%H:%M:%S"), salida["stop_reason"],
                               len(salida["content"]), time.time() - t0,
                               (" | tools: " + ", ".join(nombres)) if nombres else ""))
                        self._json(200, salida)
                        return
                    self._transmitir(r, modelo_pedido, t0)
            except urllib.error.HTTPError as e:
                # Leer el cuerpo del error puede fallar: si el tunel contesta 530 y corta la
                # conexion antes de mandar su pagina (Cloudflare tambien se cae a medias), el
                # read lanza ConnectionResetError. Sin este try, el proxy se quedaba sin
                # contestar al cliente y ZCode veia "conexion cerrada" en vez del aviso.
                try:
                    cuerpo = e.read().decode("utf-8", "replace")
                except Exception:
                    cuerpo = ""
                # El cuerpo puede ser la pagina de error de Cloudflare (7 KB de HTML): se
                # traduce a una linea con lo que hay que hacer, y a ZCode se le manda esa
                # linea, no el HTML (que en su interfaz solo es ruido ilegible).
                pista = pista_tunel(e.code, cuerpo)
                corto = pista or cuerpo[:300] or ("el tunel contesto %d sin cuerpo" % e.code)
                anotar = "HTTP %d: %s" % (e.code, pista or cuerpo[:200] or "(sin cuerpo)")
                estado.anotar_fallo(anotar)
                log("[%s] error del tunel %d: %s" % (time.strftime("%H:%M:%S"), e.code, corto))
                # 530 no es un codigo que los clientes esperen: se manda como 502 (bad gateway),
                # que es lo que de verdad ha pasado. Los 4xx del servidor se respetan.
                self._error(e.code if e.code in (400, 401, 403, 404, 429) else 502,
                            corto, "upstream_error")
            except urllib.error.URLError as e:
                # Fallo al hablar con el TUNEL (no es culpa del cliente). Se imprime la razon
                # de verdad: "connection refused" aqui suele ser un proxy del sistema muerto.
                razon = getattr(e, "reason", e)
                estado.anotar_fallo("%s: %s" % (type(razon).__name__, razon))
                log("[%s] no pude hablar con el tunel: %s: %s"
                    % (time.strftime("%H:%M:%S"), type(razon).__name__, razon))
                log("            destino: %s" % estado.upstream)
                log("            si la sesion de Kaggle es nueva, pega la URL y el TOKEN nuevos en")
                log("            http://127.0.0.1:%d/panel" % puerto)
                try:
                    self._error(502, "no pude hablar con el tunel (%s). Si la sesion de Kaggle es "
                                     "nueva, pega la URL y el TOKEN en http://127.0.0.1:%d/panel"
                                % (razon, puerto))
                except OSError:
                    pass
            except OSError as e:
                # La conexion se corto a media respuesta. Puede ser el cliente (ZCode cancela
                # y reintenta, lo normal) o el tunel cortando mientras se lee lo que manda.
                log("[%s] conexion cortada (%s): el cliente cancelo, o el tunel corto a media "
                    "respuesta" % (time.strftime("%H:%M:%S"), type(e).__name__))
                if not getattr(self, "ya_respondi", True):
                    # Todavia no se habia mandado nada (peticion normal, sin streaming): se le
                    # puede decir al cliente que ha pasado, en vez de dejarlo con la conexion
                    # cerrada. Es el caso de un 530 que corta antes de mandar su pagina.
                    estado.anotar_fallo("conexion cortada por el tunel (%s)" % type(e).__name__)
                    self._error(502, "el tunel corto la conexion antes de responder (%s). Si viene "
                                     "de un 530, el cloudflared de Kaggle se cayo: ejecuta otra vez "
                                     "la seccion 7 y pega la URL y el TOKEN nuevos en "
                                     "http://127.0.0.1:%d/panel" % (type(e).__name__, puerto))
            except Exception as e:
                log("[%s] fallo: %s: %s" % (time.strftime("%H:%M:%S"), type(e).__name__, e))
                self._error(502, "%s: %s" % (type(e).__name__, e))

        def _transmitir(self, r, modelo_pedido, t0):
            """Convierte el SSE de OpenAI en los eventos de Anthropic, en vivo."""
            self._sse_cabeceras()
            self._evento("message_start", {
                "type": "message_start",
                "message": {"id": "msg_" + uuid.uuid4().hex[:20], "type": "message",
                            "role": "assistant", "model": modelo_pedido, "content": [],
                            "stop_reason": None, "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}}})
            self._evento("ping", {"type": "ping"})

            indice = -1             # el primer bloque tiene que ser el 0 (lo exige Anthropic)
            abierto = None          # "thinking" | "text" | "tool_use"
            herramientas = {}       # posicion del tool -> {"id","name","args","_bloque"}
            nombres_tool = []
            texto_visible = ""      # para poder registrar que contesto (primeros caracteres)
            salida_tok = 0
            fin = None

            def cerrar_bloque():
                nonlocal abierto
                if abierto:
                    self._evento("content_block_stop", {"type": "content_block_stop", "index": indice})
                    abierto = None

            for linea in r:
                if not linea.startswith(b"data: "):
                    continue
                carga = linea[6:].strip()
                if carga == b"[DONE]":
                    break
                try:
                    ev = json.loads(carga)
                except Exception:
                    continue

                uso = ev.get("usage") or {}
                if uso.get("completion_tokens"):
                    salida_tok = uso["completion_tokens"]

                delta = ((ev.get("choices") or [{}])[0].get("delta")) or {}
                if ((ev.get("choices") or [{}])[0]).get("finish_reason"):
                    fin = ((ev.get("choices") or [{}])[0]).get("finish_reason")

                pensamiento = delta.get("reasoning_content")
                if pensamiento:
                    if abierto != "thinking":
                        cerrar_bloque()
                        indice += 1
                        self._evento("content_block_start", {
                            "type": "content_block_start", "index": indice,
                            "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                        abierto = "thinking"
                    self._evento("content_block_delta", {
                        "type": "content_block_delta", "index": indice,
                        "delta": {"type": "thinking_delta", "thinking": pensamiento}})

                texto = delta.get("content")
                if texto:
                    if abierto != "text":
                        cerrar_bloque()
                        indice += 1
                        self._evento("content_block_start", {
                            "type": "content_block_start", "index": indice,
                            "content_block": {"type": "text", "text": ""}})
                        abierto = "text"
                    if len(texto_visible) < 300:
                        texto_visible += texto
                    self._evento("content_block_delta", {
                        "type": "content_block_delta", "index": indice,
                        "delta": {"type": "text_delta", "text": texto}})

                for trozo in delta.get("tool_calls") or []:
                    pos = trozo.get("index", 0)
                    fn = trozo.get("function") or {}
                    est = herramientas.setdefault(pos, {"id": None, "name": "", "args": ""})
                    if trozo.get("id"):
                        est["id"] = trozo["id"]
                    if fn.get("name"):
                        est["name"] = fn["name"]
                    # El bloque se abre UNA sola vez por tool: llama.cpp manda el nombre en
                    # el primer trozo y los argumentos en muchos trozos con el mismo indice.
                    # Si se abriera un bloque por trozo, el cliente veria la misma tool
                    # repetida con el JSON partido (y el tool call se pierde).
                    if "_bloque" not in est:
                        cerrar_bloque()
                        indice += 1
                        nombres_tool.append(est["name"])
                        self._evento("content_block_start", {
                            "type": "content_block_start", "index": indice,
                            "content_block": {"type": "tool_use", "id": est["id"] or "toolu_" + uuid.uuid4().hex[:16],
                                              "name": est["name"], "input": {}}})
                        abierto = "tool_use"
                        est["_bloque"] = indice
                    if fn.get("arguments"):
                        est["args"] += fn["arguments"]
                        self._evento("content_block_delta", {
                            "type": "content_block_delta", "index": est["_bloque"],
                            "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}})

            cerrar_bloque()
            self._evento("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": parar_motivo(fin), "stop_sequence": None},
                "usage": {"output_tokens": salida_tok}})
            self._evento("message_stop", {"type": "message_stop"})
            self._fin_chunked()
            log("[%s] <- stream terminado en %.1f s | fin=%s | %d tokens%s"
                % (time.strftime("%H:%M:%S"), time.time() - t0, fin, salida_tok,
                   (" | tools: " + ", ".join(nombres_tool)) if nombres_tool else ""))
            if texto_visible.strip():
                log("            dijo: %s" % texto_visible.strip().replace("\n", " ")[:180])
            elif not nombres_tool:
                log("            (respuesta sin texto visible: solo pensamiento, o vacia)")

        do_HEAD = do_GET

    return Handler


def limpiar_url(url):
    """Acepta lo que sea que pegues: con /v1, con /v1/chat/completions, con ?token=..., o sin https://."""
    url = (url or "").strip().rstrip("/")
    if "?token=" in url:
        url = url.split("?")[0].rstrip("/")
    # Si pegaste el endpoint entero (el curl del notebook), se corta hasta el dominio.
    for sufijo in ("/v1/chat/completions", "/v1/messages", "/v1/models",
                   "/chat/completions", "/v1"):
        if url.endswith(sufijo):
            url = url[: -len(sufijo)].rstrip("/")
            break
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


def pista_tunel(codigo, cuerpo=""):
    """Traduce un error del tunel a algo corto y accionable, sin volcar el HTML de Cloudflare.

    Los que salen de verdad, con lo que hay que hacer:
      - 530 (pagina de Cloudflare): cloudflared ya no esta conectado a Kaggle -> seccion 7 otra vez.
      - 502/503/504: el tunel llega, pero el servidor de dentro no contesta -> secciones 4 y 5.
      - 403: el TOKEN no es el de esta sesion.
      - 404: la ruta no existe (o el modelo pedido no esta cargado).
    Devuelve None si no reconoce el error (entonces se ensena el cuerpo tal cual, recortado)."""
    texto = (cuerpo or "").lstrip()
    es_pagina = texto[:1] == "<" or "cloudflare" in texto[:3000].lower()
    if codigo == 403:
        return "403: el TOKEN no es el de la sesion actual de Kaggle"
    if codigo == 404:
        return ("404: el tunel no encuentra esa ruta; comprueba que llama-server siga vivo "
                "(seccion 4 del notebook)")
    if codigo == 530 or (es_pagina and codigo >= 500):
        return ("%d: el tunel existe pero Cloudflare no llega a Kaggle (se cayo el cloudflared de "
                "la sesion). En Kaggle ejecuta otra vez la seccion 7 y pega la URL y el TOKEN "
                "nuevos en el panel" % codigo)
    if codigo in (502, 503, 504):
        return ("%d: el tunel llega pero el servidor de dentro no contesta. En Kaggle mira las "
                "secciones 4 (llama-server) y 5 (proxy) y vuelve a ejecutarlas" % codigo)
    if es_pagina:
        return ("%d: el tunel devolvio una pagina de error en vez de JSON; revisa la sesion de "
                "Kaggle" % codigo)
    return None


def comprobar(upstream, token, segundos=25):
    """Avisa enseguida si la URL o el token ya no sirven, en vez de fallar luego en ZCode."""
    req = urllib.request.Request(upstream.rstrip("/") + "/v1/models",
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=segundos) as r:
            datos = json.loads(r.read())
        ids = [str(m.get("id")) for m in (datos.get("data") or [])]
        return True, "responde; modelos: %s" % (", ".join(ids[:4]) or "(ninguno)")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")[:3000]
        return False, (pista_tunel(e.code, cuerpo)
                       or "el tunel contesto HTTP %d: %s" % (e.code, cuerpo[:200]))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def main():
    ap = argparse.ArgumentParser(
        description="Traduce el protocolo de Anthropic (ZCode) al de OpenAI (tu tunel de Kaggle).",
        epilog="La URL y el TOKEN los imprime la seccion 7 del notebook. Se recuerdan solos: "
               "la proxima vez basta con ejecutar el script sin nada mas.")
    ap.add_argument("--upstream", help="URL del tunel: https://algo.trycloudflare.com")
    ap.add_argument("--token", help="TOKEN del tunel")
    ap.add_argument("--puerto", type=int, default=8081)
    ap.add_argument("--modelo", default="razonamiento",
                    help="id del modelo en el tunel (el ALIAS del notebook)")
    ap.add_argument("--limite-tokens", type=int, default=None,
                    help="tope de tokens de entrada antes de recortar el historial viejo. Por "
                         "defecto se le pregunta al servidor su contexto (-c) y se deja un "
                         "margen de 2048; si no responde, 30000")
    ap.add_argument("--sin-comprobar", action="store_true",
                    help="no comprobar el tunel antes de arrancar")
    args = ap.parse_args()

    upstream, token = args.upstream, args.token

    # Si no los pasas, se usan los de la ultima vez.
    if (not upstream or not token) and os.path.exists(GUARDADO):
        try:
            with open(GUARDADO, encoding="utf-8") as f:
                guardado = json.load(f)
            upstream = upstream or guardado.get("upstream")
            token = token or guardado.get("token")
            if upstream and token:
                print("Uso la URL y el TOKEN guardados en %s" % os.path.basename(GUARDADO))
                print("(si la sesion de Kaggle es nueva, esos ya no sirven: pasalos de nuevo)")
        except Exception:
            pass

    if not upstream or not token:
        print("Faltan la URL del tunel y el TOKEN.")
        print()
        print("Los dos los imprime la seccion 7 del notebook de Kaggle. Se pasan asi:")
        print()
        print("  python zcode-anthropic-proxy.py --upstream https://algo.trycloudflare.com --token ABC123")
        print()
        print("Quedan guardados, asi que la proxima vez (misma sesion) basta con:")
        print()
        print("  python zcode-anthropic-proxy.py")
        print()
        print("Y si la sesion de Kaggle se cerro, la URL y el token viejos ya no sirven: abre el")
        print("notebook, ejecuta las secciones 2, 4, 5, 6, 7 y 9, y pasa los nuevos.")
        return 1

    upstream = limpiar_url(upstream)
    if not upstream:
        print("La URL no parece valida.")
        return 1

    estado = Estado(upstream, token, args.modelo, args.limite_tokens)
    # Se guarda ya: si el tunel no responde, al menos no hay que reescribirlos.
    estado.guardar()

    if not args.sin_comprobar:
        print("Comprobando el tunel...")
        ok, detalle = estado.comprobar()
        print("  %s" % detalle)
        if not ok:
            print("  (arranco igualmente: a veces el modelo aun esta cargandose en la GPU)")

    # El tope de recorte se pregunta al servidor: asi el mismo proxy vale con -c 32768 o con
    # -c 131072 sin tocar nada. Ojo: las herramientas de ZCode ocupan mucho prompt (53 tools
    # ~26k tokens), asi que cuanto mas contexto tenga el servidor, menos hay que recortar.
    if args.limite_tokens is None:
        ctx = estado.contexto()
        if ctx:
            estado.limite_tokens = max(4096, ctx - 2048)
            print("  contexto del servidor: %d tokens -> el recorte se pone en %d"
                  % (ctx, estado.limite_tokens))
        else:
            # Antes solo lo decia: limite_tokens se quedaba en None y el arranque petaba al
            # imprimir el banner (TypeError con %d) o al primer recorte.
            estado.limite_tokens = 30000
            print("  no pude preguntar el contexto al servidor; dejo el recorte en 30000")

    Handler = crear_handler(estado, args.puerto)

    class Servidor(socketserver.ThreadingTCPServer):
        # En Windows SO_REUSEADDR deja que DOS procesos escuchen en el mismo puerto y el
        # sistema reparte las conexiones entre ellos: la mitad de las peticiones acabarian en
        # una instancia vieja, apuntando a un tunel muerto. Mejor que la segunda falle claro.
        allow_reuse_address = os.name != "nt"
        daemon_threads = True

        def handle_error(self, request, client_address):
            """Un cliente que corta la conexion no es un fallo del proxy.

            ZCode cancela peticiones y el navegador abre conexiones de mas y las cierra sin
            mandar nada: socketserver lo imprime como un traceback de veinte lineas
            (ConnectionAbortedError 10053, ConnectionResetError 10054, BrokenPipeError) y
            parece que el proxy se esta rompiendo, cuando no ha pasado nada. Esos casos se
            callan; si es otro error, una linea con la causa en vez del traceback entero."""
            error = sys.exc_info()[1]
            if isinstance(error, (ConnectionResetError, ConnectionAbortedError,
                                  BrokenPipeError, TimeoutError)):
                return
            print("[%s] error atendiendo a %s: %s: %s"
                  % (time.strftime("%H:%M:%S"), client_address, type(error).__name__, error),
                  file=sys.stderr, flush=True)

    # El puerto puede estar todavia ocupado por el proxy anterior: al cerrarlo (o al matarlo)
    # Windows tarda un momento en soltarlo, y antes esto fallaba al primer intento con un
    # "ya habra otro proxy" que no era verdad. Se reintenta unos segundos.
    servidor = None
    ultimo = None
    for intento in range(10):
        try:
            servidor = Servidor(("127.0.0.1", args.puerto), Handler)
            break
        except OSError as e:
            ultimo = e
            if intento == 0:
                print("El puerto %d esta ocupado; espero a que termine de cerrarse el proxy "
                      "anterior..." % args.puerto)
            time.sleep(0.6)
    if servidor is None:
        print("No pude escuchar en el puerto %d: %s" % (args.puerto, ultimo))
        print("Suele ser un proxy anterior a medio cerrar: cierra su ventana, o mira")
        print("  tasklist | findstr python")
        print("y mata el que sobre. Tambien puedes arrancar con --puerto otro_numero.")
        return 1

    print()
    print("Proxy Anthropic -> OpenAI escuchando en http://127.0.0.1:%d" % args.puerto)
    print("  destino : %s/v1" % estado.upstream)
    print("  modelo  : %s" % estado.modelo)
    print("  recorte : la sesion se ajusta a ~%d tokens SIN borrar nunca el system ni la pregunta"
          % estado.limite_tokens)
    print()
    print("En ZCode el proveedor apunta a:  http://127.0.0.1:%d/v1" % args.puerto)
    print("Panel (pega ahi la URL y el TOKEN nuevos de Kaggle):  http://127.0.0.1:%d/panel"
          % args.puerto)
    print("Deja esta ventana abierta mientras uses el modelo. Ctrl+C para parar.")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print()
        print("Parando.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
