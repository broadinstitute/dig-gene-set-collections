from __future__ import annotations

import mimetypes
import sqlite3
import subprocess
from pathlib import Path
from urllib import error, parse, request

from flask import Response


DB_PATH = Path(__file__).resolve().parents[1] / "data" / "genseco_v20260528.sqlite"


def _html_error(message: str, status_code: int) -> Response:
    body = f"<html><body><p>{message}</p></body></html>"
    return Response(body, status=status_code, mimetype="text/html")


def _get_provenance_node_url(provenance_node_id: int) -> str | None:
    connection = sqlite3.connect(DB_PATH)

    try:
        row = connection.execute(
            """
            SELECT dcc_url
            FROM provenance_node
            WHERE provenance_node_id = ?
            """,
            (provenance_node_id,),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        connection.close()


def _guess_mimetype(filename: str) -> str:
    if filename.lower().endswith(".gmt"):
        return "text/plain"

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _s3_to_https(url: str) -> tuple[str, str]:
    parsed = parse.urlparse(url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    quoted_key = parse.quote(key)
    filename = Path(key).name or "download"
    return f"https://{bucket}.s3.amazonaws.com/{quoted_key}", filename


def _fetch_s3_with_aws_cli(url: str) -> Response:
    filename = Path(parse.urlparse(url).path).name or "download"
    mimetype = _guess_mimetype(filename)

    try:
        completed = subprocess.run(
            ["aws", "s3", "cp", url, "-", "--no-progress"],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return _html_error("unable to fetch s3 object: aws CLI is not installed", 500)
    except PermissionError as exc:
        return _html_error(f"unable to fetch {url}: permission denied ({exc})", 403)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip() or f"aws exited with code {exc.returncode}"
        if "AccessDenied" in stderr or "403" in stderr:
            status_code = 403
        elif "NoSuchKey" in stderr or "404" in stderr:
            status_code = 404
        else:
            status_code = 502
        return _html_error(f"unable to fetch {url}: {stderr}", status_code)

    response = Response(completed.stdout, status=200, mimetype=mimetype)
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def _build_success_response(
    body: bytes,
    source_url: str,
    content_type: str | None,
    content_disposition: str | None,
) -> Response:
    filename = Path(parse.urlparse(source_url).path).name or "download"
    mimetype = content_type or _guess_mimetype(filename)

    response = Response(body, status=200, mimetype=mimetype)
    response.headers["Content-Disposition"] = content_disposition or f'inline; filename="{filename}"'
    return response


def fetch_provenance_node_content(provenance_node_id: int) -> Response:
    dcc_url = _get_provenance_node_url(provenance_node_id)
    if dcc_url is None:
        return _html_error(f"provenance_node_id {provenance_node_id} not found", 404)

    if dcc_url.startswith("s3://"):
        return _fetch_s3_with_aws_cli(dcc_url)
    elif dcc_url.startswith("http://") or dcc_url.startswith("https://"):
        fetch_url = dcc_url
    else:
        return _html_error("<invalid operation>", 400)

    try:
        with request.urlopen(fetch_url) as remote_response:
            body = remote_response.read()
            content_type = remote_response.headers.get_content_type()
            content_disposition = remote_response.headers.get("Content-Disposition")
            return _build_success_response(body, fetch_url, content_type, content_disposition)
    except error.HTTPError as exc:
        return _html_error(f"unable to fetch {dcc_url}: HTTP {exc.code}", exc.code)
    except error.URLError as exc:
        return _html_error(f"unable to fetch {dcc_url}: {exc.reason}", 502)
    except PermissionError as exc:
        return _html_error(f"unable to fetch {dcc_url}: permission denied ({exc})", 403)
    except OSError as exc:
        return _html_error(f"unable to fetch {dcc_url}: {exc}", 500)
