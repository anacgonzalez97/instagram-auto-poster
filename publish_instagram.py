# publish_instagram.py
import os
import io
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
#  El codigo las lee automaticamente de las variables de entorno.
# ==================================================================
# Carga las credenciales desde los Secrets de GitHub (si existen)
GOOGLE_CREDS_JSON   = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
IG_USER_ID          = os.environ.get("IG_USER_ID", "")
IG_ACCESS_TOKEN     = os.environ.get("IG_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

# IDs fijas de Google Drive (Pega tus IDs reales entre las comillas)
DRIVE_FOLDER_ID     = "1cSK8eSFQ88nFdERpEDJ2gRa1asRv_Db5"
PROCESSED_FOLDER_ID = "1c4QKFgRqWJKg4tv3AbCLwM6sif-A5iTo"

# Opciones por defecto
PUBLISH_TYPE        = os.environ.get("PUBLISH_TYPE", "POST").upper()
SELECT_MODE         = os.environ.get("SELECT_MODE", "FIRST").upper()

GRAPH_API_VERSION   = "v21.0"
GRAPH_BASE          = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
SCOPES              = ["https://www.googleapis.com/auth/drive"]

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


def pick_image(service):
    """Selecciona una imagen de la carpeta de origen."""
    # Reemplaza la cadena de abajo por el ID real de tu carpeta de Drive:
    folder_id = "1cSK8eSFQ88nFdERpEDJ2gRa1asRv_Db5"

    query = (
        f"'{folder_id}' in parents "
        f"and trashed = false "
        f"and (mimeType = 'image/jpeg' or mimeType = 'image/png')"
    )
    try:
        resp = service.files().list(
            q=query,
            fields="files(id, name, mimeType)",
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
    except HttpError as e:
        log.error("Error consultando la carpeta de Drive: %s", e)
        raise

    files = resp.get("files", [])
    if not files:
        log.warning("No hay imagenes disponibles en la carpeta. Nada que publicar.")
        return None

    if SELECT_MODE == "RANDOM":
        chosen = random.choice(files)
    else:
        chosen = sorted(files, key=lambda f: f["name"])[0]

    log.info("Imagen seleccionada: %s (%s)", chosen["name"], chosen["id"])
    return chosen


def download_image_bytes(service, file_id):
    """Descarga el binario de la imagen desde Drive (para pasarselo a Claude)."""
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()


def make_public_temporarily(service, file_id):
    """Crea un permiso publico de lectura y devuelve la URL de descarga directa."""
    try:
        permission = service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
            fields="id",
        ).execute()
        log.info("Permiso publico temporal creado: %s", permission["id"])
    except HttpError as e:
        log.error("No se pudo crear el permiso publico: %s", e)
        raise

    public_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    return public_url, permission["id"]


def revoke_public(service, file_id, permission_id):
    """Elimina el permiso publico (limpieza de seguridad)."""
    try:
        service.permissions().delete(
            fileId=file_id,
            permissionId=permission_id,
            supportsAllDrives=True,
        ).execute()
        log.info("Permiso publico revocado.")
    except HttpError as e:
        log.warning("No se pudo revocar el permiso publico (revisar manualmente): %s", e)


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
# CAPTION
# ==================================================================
def build_caption(filename):
    """Plantilla simple de respaldo basada en el nombre del archivo."""
    base = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ").strip()
    hashtags = "#foto #instadaily #photography #dailypost"
    return f"{base.capitalize()}\n\n{hashtags}"


def build_caption_ai(service, file_id, filename, mime_type):
    """Genera un caption con Claude analizando la imagen. Cae a plantilla si falla."""
    if not ANTHROPIC_API_KEY:
        log.warning("Sin ANTHROPIC_API_KEY, uso plantilla simple.")
        return build_caption(filename)

    try:
        img_bytes = download_image_bytes(service, file_id)
        img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        media_type = "image/png" if mime_type == "image/png" else "image/jpeg"

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="cclaude-sonnet-4-5",
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
def create_media_container(image_url, caption):
    """Paso 1: crear el contenedor de medios."""
    endpoint = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    params = {"image_url": image_url, "access_token": IG_ACCESS_TOKEN}

    if PUBLISH_TYPE == "STORY":
        params["media_type"] = "STORIES"
    else:
        params["caption"] = caption

    resp = requests.post(endpoint, data=params, timeout=60)
    data = resp.json()
    if resp.status_code != 200 or "id" not in data:
        raise RuntimeError(f"Error creando el contenedor: {data}")
    log.info("Contenedor creado: %s", data["id"])
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
    log.info("=== Inicio del proceso de publicacion (%s) ===", PUBLISH_TYPE)
    service = get_drive_service()

    chosen = pick_image(service)
    if not chosen:
        log.info("Sin imagenes. Fin sin error.")
        return

    file_id = chosen["id"]
    permission_id = None
    published = False

    try:
        public_url, permission_id = make_public_temporarily(service, file_id)
        caption = build_caption_ai(service, file_id, chosen["name"], chosen["mimeType"])

        container_id = create_media_container(public_url, caption)
        wait_until_ready(container_id)
        publish_container(container_id)
        published = True
    finally:
        if permission_id:
            revoke_public(service, file_id, permission_id)

    if published:
        finalize_file(service, file_id)

    refresh_meta_token()
    log.info("=== Proceso finalizado ===")


if __name__ == "__main__":
    main()
