# publish_instagram.py
from PIL import Image
import os
import io
import re
import sys
import json
import time
import base64
import random
import logging
import requests

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
from nacl import encoding, public
import anthropic

# ------------------------------------------------------------------
# Configuracion de logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ig-publisher")

# ==================================================================
#  >>> AQUI NO SE PONE NADA. Todas las claves vienen de GitHub Secrets.
# ==================================================================
GOOGLE_CREDS_JSON   = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
IG_USER_ID          = os.environ.get("IG_USER_ID", "")
IG_ACCESS_TOKEN     = os.environ.get("IG_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

# IDs fijas de Google Drive
DRIVE_FOLDER_ID     = "1cSK8eSFQ88nFdERpEDJ2gRa1asRv_Db5"
PROCESSED_FOLDER_ID = "1c4QKFgRqWJKg4tv3AbCLwM6sif-A5iTo"

# Opciones por defecto
PUBLISH_TYPE        = os.environ.get("PUBLISH_TYPE", "AUTO").upper()  # AUTO / STORY
SELECT_MODE         = os.environ.get("SELECT_MODE", "FIRST").upper()

GRAPH_API_VERSION   = "v21.0"
GRAPH_BASE          = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
SCOPES              = ["https://www.googleapis.com/auth/drive"]

# Formato de nombre para agrupar carruseles:  "01-1.jpg", "01-02.png", ...
GROUP_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)")


# ==================================================================
# GOOGLE DRIVE
# ==================================================================
def get_drive_service():
    """Autentica con la service account y devuelve el cliente de Drive."""
    try:
        info = json.loads(GOOGLE_CREDS_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        log.error("No se pudo autenticar en Google Drive: %s", e)
        raise


def list_images(service):
    """Lista todas las imagenes de la carpeta de origen."""
    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        f"and trashed = false "
        f"and (mimeType = 'image/jpeg' or mimeType = 'image/png')"
    )
    try:
        resp = service.files().list(
            q=query,
            fields="files(id, name, mimeType)",
            pageSize=200,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
    except HttpError as e:
        log.error("Error consultando la carpeta de Drive: %s", e)
        raise
    return resp.get("files", [])


def pick_group(service):
    """Devuelve la lista de archivos del siguiente grupo a publicar.

    Convencion de nombres:
        01-1.jpg, 01-2.jpg, 01-3.jpg  -> carrusel del grupo 01 (3 imagenes)
        02-1.jpg, 02-2.jpg            -> carrusel del grupo 02 (2 imagenes)
        03-1.jpg                      -> post individual
        playa.jpg                     -> sin numerar, post individual

    Se publica siempre el grupo con el prefijo mas bajo.
    Devuelve 1 archivo -> POST individual. 2-10 -> CAROUSEL.
    """
    files = list_images(service)
    if not files:
        log.warning("No hay imagenes disponibles en la carpeta. Nada que publicar.")
        return []

    grupos = {}      # prefijo -> [(orden, file)]
    sueltas = []
    for f in files:
        m = GROUP_RE.match(f["name"])
        if m:
            grupos.setdefault(int(m.group(1)), []).append((int(m.group(2)), f))
        else:
            sueltas.append(f)

    if grupos:
        prefijo = min(grupos)
        items = sorted(grupos[prefijo], key=lambda t: t[0])
        chosen = [f for _, f in items][:10]   # IG permite maximo 10
        log.info(
            "Grupo %s -> %d imagen(es): %s",
            prefijo, len(chosen), [f["name"] for f in chosen],
        )
        return chosen

    # No hay nada numerado: publicamos una imagen suelta
    if SELECT_MODE == "RANDOM":
        chosen = random.choice(sueltas)
    else:
        chosen = sorted(sueltas, key=lambda f: f["name"])[0]
    log.info("Sin grupos numerados. Imagen suelta: %s", chosen["name"])
    return [chosen]


def download_image_bytes(service, file_id):
    """Descarga el binario de la imagen desde Drive."""
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()


def finalize_file(service, file_id):
    """Mueve el archivo a la carpeta de procesadas o lo borra."""
    if PROCESSED_FOLDER_ID:
        try:
            file = service.files().get(
                fileId=file_id, fields="parents", supportsAllDrives=True
            ).execute()
            prev_parents = ",".join(file.get("parents", []))
            service.files().update(
                fileId=file_id,
                addParents=PROCESSED_FOLDER_ID,
                removeParents=prev_parents,
                supportsAllDrives=True,
                fields="id, parents",
            ).execute()
            log.info("Archivo movido a la carpeta de procesadas.")
        except HttpError as e:
            log.error("No se pudo mover el archivo: %s", e)
            raise
    else:
        try:
            service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
            log.info("Archivo borrado de Drive.")
        except HttpError as e:
            log.error("No se pudo borrar el archivo: %s", e)
            raise


# ==================================================================
# PROCESADO DE IMAGEN (recorte inteligente)
# ==================================================================
def smart_crop_bytes(img_bytes, force_ratio=None):
    """Recorta a un ratio valido de IG (4:5 vertical, 1.91:1 horizontal).

    Si se pasa force_ratio (ancho/alto), recorta exactamente a ese ratio.
    Se usa en carruseles para que todas las imagenes compartan proporcion,
    porque IG aplica el ratio de la primera a todas las demas.
    """
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = im.size
    ratio = w / h

    MIN_R, MAX_R = 4 / 5, 1.91  # limites de IG

    if force_ratio:
        target_r = force_ratio
        if ratio > target_r:
            # sobra ancho -> recortar lados, centrado
            new_w = int(h * target_r)
            left = (w - new_w) // 2
            target = im.crop((left, 0, left + new_w, h))
        elif ratio < target_r:
            # sobra alto -> recortar altura, anclado arriba (conserva cara)
            new_h = int(w / target_r)
            top = int((h - new_h) * 0.15)
            target = im.crop((0, top, w, top + new_h))
        else:
            target = im
    elif MIN_R <= ratio <= MAX_R:
        target = im  # ya es valida
    elif ratio < MIN_R:
        # muy vertical -> recortar altura a 4:5, anclado arriba, corta pies
        new_h = int(w / MIN_R)
        top = int((h - new_h) * 0.15)
        target = im.crop((0, top, w, top + new_h))
    else:
        # muy horizontal -> recortar ancho a 1.91:1, centrado
        new_w = int(h * MAX_R)
        left = (w - new_w) // 2
        target = im.crop((left, 0, left + new_w, h))

    out = io.BytesIO()
    target.save(out, format="JPEG", quality=90)
    out.seek(0)
    return out.read()


def decide_carousel_ratio(img_bytes):
    """Elige el ratio comun del carrusel a partir de la PRIMERA imagen."""
    im = Image.open(io.BytesIO(img_bytes))
    w, h = im.size
    r = w / h
    if r < 0.95:
        return 4 / 5      # vertical
    if r > 1.2:
        return 1.91       # horizontal
    return 1.0            # cuadrado


# ==================================================================
# HOST DE IMAGEN EN GITHUB (branch 'media')
# ==================================================================
def upload_to_github(img_bytes, filename):
    """Sube la imagen procesada al repo (branch 'media') y devuelve URL raw publica."""
    gh_token = os.environ["GH_PAT"]
    gh_repo  = os.environ["GITHUB_REPOSITORY"]
    branch   = "media"
    path     = f"tmp/{int(time.time())}_{filename}.jpg"

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    content_b64 = base64.standard_b64encode(img_bytes).decode()
    r = requests.put(
        f"https://api.github.com/repos/{gh_repo}/contents/{path}",
        headers=headers,
        json={"message": f"media {path}", "content": content_b64, "branch": branch},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Error subiendo a GitHub: {r.text}")
    return f"https://raw.githubusercontent.com/{gh_repo}/{branch}/{path}", path


def cleanup_github(path):
    """Borra el archivo temporal de la branch media tras publicar."""
    try:
        gh_token = os.environ["GH_PAT"]
        gh_repo  = os.environ["GITHUB_REPOSITORY"]
        headers = {"Authorization": f"Bearer {gh_token}",
                   "Accept": "application/vnd.github+json"}
        meta = requests.get(
            f"https://api.github.com/repos/{gh_repo}/contents/{path}?ref=media",
            headers=headers, timeout=30).json()
        requests.delete(
            f"https://api.github.com/repos/{gh_repo}/contents/{path}",
            headers=headers,
            json={"message": f"cleanup {path}", "sha": meta["sha"], "branch": "media"},
            timeout=30)
    except Exception as e:
        log.warning("No se pudo limpiar %s: %s", path, e)


# ==================================================================
# CAPTION
# ==================================================================
def clean_name_for_caption(filename):
    """Quita el prefijo de grupo (01-2) y la extension del nombre."""
    base = os.path.splitext(filename)[0]
    base = GROUP_RE.sub("", base)
    return base.replace("_", " ").replace("-", " ").strip()


def build_caption(filename):
    """Plantilla simple de respaldo basada en el nombre del archivo."""
    base = clean_name_for_caption(filename)
    hashtags = "#foto #instadaily #photography #dailypost"
    if not base:
        return hashtags
    return f"{base.capitalize()}\n\n{hashtags}"


def build_caption_ai(service, file_id, filename, mime_type, img_bytes=None, extra=""):
    """Genera un caption con Claude analizando la imagen. Cae a plantilla si falla."""
    if not ANTHROPIC_API_KEY:
        log.warning("Sin ANTHROPIC_API_KEY, uso plantilla simple.")
        return build_caption(filename)

    try:
        if img_bytes is None:
            img_bytes = download_image_bytes(service, file_id)
        img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        media_type = "image/png" if mime_type == "image/png" else "image/jpeg"

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": img_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Escribe un caption atractivo para Instagram en espanol "
                                "sobre esta imagen. Maximo 2 frases, tono cercano. "
                                + extra +
                                "Anade una linea en blanco y luego 5-8 hashtags relevantes. "
                                "Devuelve SOLO el caption, sin comillas ni explicaciones."
                            ),
                        },
                    ],
                }
            ],
        )
        caption = message.content[0].text.strip()
        log.info("Caption generado con IA.")
        return caption
    except Exception as e:
        log.warning("Fallo la generacion con IA (%s). Uso plantilla simple.", e)
        return build_caption(filename)


# ==================================================================
# INSTAGRAM GRAPH API
# ==================================================================
def create_media_container(image_url, caption, as_story=False):
    """Paso 1 (POST/STORY): crear el contenedor de medios."""
    endpoint = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    params = {"image_url": image_url, "access_token": IG_ACCESS_TOKEN}

    if as_story:
        params["media_type"] = "STORIES"
    else:
        params["caption"] = caption

    resp = requests.post(endpoint, data=params, timeout=60)
    data = resp.json()
    if resp.status_code != 200 or "id" not in data:
        raise RuntimeError(f"Error creando el contenedor: {data}")
    log.info("Contenedor creado: %s", data["id"])
    return data["id"]


def create_carousel_item(image_url):
    """Contenedor hijo de un carrusel (sin caption)."""
    endpoint = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    params = {
        "image_url": image_url,
        "is_carousel_item": "true",
        "access_token": IG_ACCESS_TOKEN,
    }
    resp = requests.post(endpoint, data=params, timeout=60)
    data = resp.json()
    if resp.status_code != 200 or "id" not in data:
        raise RuntimeError(f"Error creando item de carrusel: {data}")
    log.info("Item de carrusel creado: %s", data["id"])
    return data["id"]


def create_carousel_container(children_ids, caption):
    """Contenedor padre CAROUSEL que agrupa los hijos."""
    endpoint = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    params = {
        "media_type": "CAROUSEL",
        "children": ",".join(children_ids),
        "caption": caption,
        "access_token": IG_ACCESS_TOKEN,
    }
    resp = requests.post(endpoint, data=params, timeout=60)
    data = resp.json()
    if resp.status_code != 200 or "id" not in data:
        raise RuntimeError(f"Error creando carrusel: {data}")
    log.info("Carrusel creado: %s", data["id"])
    return data["id"]


def wait_until_ready(container_id, max_attempts=20, delay=5):
    """Paso 2: esperar a que Meta descargue y procese la imagen."""
    endpoint = f"{GRAPH_BASE}/{container_id}"
    for attempt in range(1, max_attempts + 1):
        resp = requests.get(
            endpoint,
            params={"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN},
            timeout=30,
        )
        data = resp.json()
        status = data.get("status_code")
        log.info("Estado del contenedor (intento %d/%d): %s", attempt, max_attempts, status)

        if status == "FINISHED":
            return True
        if status == "ERROR":
            raise RuntimeError(f"El contenedor fallo al procesarse: {data}")
        time.sleep(delay)

    raise TimeoutError("El contenedor no llego a estado FINISHED a tiempo.")


def publish_container(container_id):
    """Paso 3: publicar el contenedor."""
    endpoint = f"{GRAPH_BASE}/{IG_USER_ID}/media_publish"
    resp = requests.post(
        endpoint,
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
        timeout=60,
    )
    data = resp.json()
    if resp.status_code != 200 or "id" not in data:
        raise RuntimeError(f"Error publicando: {data}")
    log.info("Publicado con exito! Media ID: %s", data["id"])
    return data["id"]


# ==================================================================
# REFRESCO DEL TOKEN DE META
# ==================================================================
def refresh_meta_token():
    """Renueva el token largo de Meta y lo guarda de vuelta como secret de GitHub."""
    app_id = os.environ.get("META_APP_ID")
    app_secret = os.environ.get("META_APP_SECRET")
    gh_token = os.environ.get("GH_PAT")
    gh_repo = os.environ.get("GITHUB_REPOSITORY")

    if not all([app_id, app_secret, gh_token, gh_repo]):
        log.info("Faltan variables para refrescar token. Se omite el refresco.")
        return

    try:
        resp = requests.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": IG_ACCESS_TOKEN,
            },
            timeout=30,
        )
        data = resp.json()
        new_token = data.get("access_token")
        if not new_token:
            log.warning("No se obtuvo token nuevo: %s", data)
            return
        log.info("Token de Meta renovado correctamente.")
    except Exception as e:
        log.warning("Error intercambiando token: %s", e)
        return

    try:
        headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
        }
        key_resp = requests.get(
            f"https://api.github.com/repos/{gh_repo}/actions/secrets/public-key",
            headers=headers, timeout=30,
        ).json()

        pub_key = public.PublicKey(key_resp["key"].encode(), encoding.Base64Encoder())
        sealed = public.SealedBox(pub_key).encrypt(new_token.encode())
        encrypted_value = encoding.Base64Encoder().encode(sealed).decode()

        put_resp = requests.put(
            f"https://api.github.com/repos/{gh_repo}/actions/secrets/IG_ACCESS_TOKEN",
            headers=headers,
            json={"encrypted_value": encrypted_value, "key_id": key_resp["key_id"]},
            timeout=30,
        )
        if put_resp.status_code in (201, 204):
            log.info("Secret IG_ACCESS_TOKEN actualizado en GitHub.")
        else:
            log.warning("No se pudo actualizar el secret: %s", put_resp.text)
    except Exception as e:
        log.warning("Error guardando el token en GitHub: %s", e)


# ==================================================================
# FLUJO PRINCIPAL
# ==================================================================
def main():
    log.info("=== Inicio (%s) ===", PUBLISH_TYPE)
    service = get_drive_service()

    chosen = pick_group(service)
    if not chosen:
        log.info("Sin imagenes. Fin.")
        return

    as_story = (PUBLISH_TYPE == "STORY")
    if as_story:
        chosen = chosen[:1]   # una story = una imagen
        log.info("Modo STORY forzado, solo se usa: %s", chosen[0]["name"])

    es_carrusel = (not as_story) and len(chosen) >= 2
    log.info("Modo: %s (%d imagen/es)",
             "CAROUSEL" if es_carrusel else ("STORY" if as_story else "POST"),
             len(chosen))

    gh_paths, child_ids = [], []
    container_id = None
    published = False

    try:
        # Descargamos la primera para decidir el ratio comun y generar el caption
        first_raw = download_image_bytes(service, chosen[0]["id"])
        force_ratio = decide_carousel_ratio(first_raw) if es_carrusel else None
        if force_ratio:
            log.info("Ratio comun del carrusel: %.3f", force_ratio)

        caption = ""
        if not as_story:
            extra = ("Es la primera imagen de un carrusel de "
                     f"{len(chosen)} fotos. " if es_carrusel else "")
            caption = build_caption_ai(
                service,
                chosen[0]["id"],
                chosen[0]["name"],
                chosen[0]["mimeType"],
                img_bytes=first_raw,
                extra=extra,
            )

        for i, f in enumerate(chosen):
            raw = first_raw if i == 0 else download_image_bytes(service, f["id"])
            cropped = smart_crop_bytes(raw, force_ratio=force_ratio)
            url, gh_path = upload_to_github(cropped, f["id"])
            gh_paths.append(gh_path)
            log.info("Subida %d/%d: %s", i + 1, len(chosen), f["name"])

            if es_carrusel:
                child_ids.append(create_carousel_item(url))
            else:
                container_id = create_media_container(url, caption, as_story=as_story)

        if es_carrusel:
            container_id = create_carousel_container(child_ids, caption)

        wait_until_ready(container_id)
        publish_container(container_id)
        published = True
    finally:
        for p in gh_paths:
            cleanup_github(p)

    if published:
        for f in chosen:
            finalize_file(service, f["id"])

    refresh_meta_token()
    log.info("=== Fin ===")


if __name__ == "__main__":
    main()
