# -*- coding: utf-8 -*-

PLUGIN_NAME = "Arabic Sort Bridge"
PLUGIN_AUTHOR = "Custom local integration"
PLUGIN_DESCRIPTION = (
    "Sends one selected Picard cluster or album to Arabic Sort, opens the "
    "review page, and accepts reviewed per-track titles back into Picard."
)
PLUGIN_VERSION = "1.2.0"
PLUGIN_API_VERSIONS = ["2.2"]
PLUGIN_LICENSE = "GPL-2.0-or-later"
PLUGIN_LICENSE_URL = "https://www.gnu.org/licenses/gpl-2.0.html"

import json
import os
import re
import secrets
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from PyQt5 import QtCore, QtGui, QtWidgets

from picard import log
from picard.album import Album
from picard.cluster import Cluster
from picard.ui.itemviews import (
    BaseAction,
    register_album_action,
    register_cluster_action,
)


API_URL = os.getenv(
    "ARABIC_SORT_API_URL",
    "http://arabic-sort:8787",
).rstrip("/")

PUBLIC_URL = os.getenv(
    "ARABIC_SORT_PUBLIC_URL",
    "http://192.168.50.195:8787",
).rstrip("/")

REQUEST_TIMEOUT_SECONDS = 15
POLL_TIMEOUT_SECONDS = 10

try:
    POLL_INTERVAL_SECONDS = max(
        0.5,
        float(os.getenv("ARABIC_SORT_POLL_INTERVAL", "1")),
    )
except ValueError:
    POLL_INTERVAL_SECONDS = 1.0

_ACTIVE_LOCK = threading.RLock()
_ACTIVE_SESSION = None


class _BridgeSignals(QtCore.QObject):
    command_received = QtCore.pyqtSignal(object)


_BRIDGE_SIGNALS = _BridgeSignals()


def _number(value, default=999999):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else default


def _metadata_value(metadata, *keys):
    for key in keys:
        try:
            value = metadata.get(key, "")
        except AttributeError:
            value = metadata[key] if key in metadata else ""
        if value:
            return str(value).strip()
    return ""


def _ordered_items(items):
    return sorted(
        list(items),
        key=lambda item: (
            _number(_metadata_value(item.metadata, "discnumber"), 1),
            _number(_metadata_value(item.metadata, "tracknumber")),
            _metadata_value(item.metadata, "title").casefold(),
        ),
    )


def _build_cluster_payload(cluster):
    files = _ordered_items(cluster.files)
    if not files:
        raise ValueError("The selected cluster contains no files.")

    artist = _metadata_value(cluster.metadata, "albumartist", "artist")
    if not artist:
        artist = _metadata_value(files[0].metadata, "albumartist", "artist")

    album = _metadata_value(cluster.metadata, "album")
    if not album:
        album = _metadata_value(files[0].metadata, "album")

    titles = []
    tracks = []
    targets = {}

    for position, file_obj in enumerate(files, start=1):
        title = _metadata_value(file_obj.metadata, "title")
        if not title:
            raise ValueError(
                "Track %d in the selected cluster has no title tag." % position
            )
        row_id = secrets.token_urlsafe(16)
        titles.append(title)
        tracks.append({"row_id": row_id, "title": title})
        targets[row_id] = {"kind": "file", "object": file_obj}

    payload = {
        "artist": artist,
        "album": album,
        "titles": titles,
        "tracks": tracks,
        "source": "cluster",
    }
    return payload, targets


def _build_album_payload(album_obj):
    tracks_in_album = _ordered_items(album_obj.tracks)
    if not tracks_in_album:
        raise ValueError("The selected MusicBrainz album contains no tracks.")

    artist = _metadata_value(album_obj.metadata, "albumartist", "artist")
    album = _metadata_value(album_obj.metadata, "album")

    titles = []
    tracks = []
    targets = {}

    for position, track in enumerate(tracks_in_album, start=1):
        title = _metadata_value(track.metadata, "title")
        if not title:
            raise ValueError(
                "Track %d in the selected album has no title." % position
            )
        row_id = secrets.token_urlsafe(16)
        titles.append(title)
        tracks.append({"row_id": row_id, "title": title})
        targets[row_id] = {"kind": "track", "object": track}

    payload = {
        "artist": artist,
        "album": album,
        "titles": titles,
        "tracks": tracks,
        "source": "album",
    }
    return payload, targets


def _extract_payload_and_targets(obj):
    if isinstance(obj, Cluster):
        return _build_cluster_payload(obj)
    if isinstance(obj, Album):
        return _build_album_payload(obj)
    raise ValueError("Select one Picard cluster or one MusicBrainz album.")


def _request_json(url, *, method="GET", payload=None, token=None, timeout=15):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = "Bearer " + token

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP %s: %s" % (exc.code, body))
    except URLError as exc:
        raise RuntimeError("Connection failed: %s" % exc.reason)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("Arabic Sort returned invalid JSON: %s" % raw)


def _create_session(payload):
    result = _request_json(
        API_URL + "/picard/prefill",
        method="POST",
        payload=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    if not result.get("ok"):
        raise RuntimeError(
            "Arabic Sort did not create a session: %s"
            % result.get("detail", result)
        )

    path = str(result.get("path") or "").strip()
    if path:
        session_url = urljoin(PUBLIC_URL + "/", path.lstrip("/"))
    else:
        session_url = str(result.get("url") or "").strip()

    session_id = str(result.get("session_id") or "").strip()
    picard_token = str(result.get("picard_token") or "").strip()

    if not session_url:
        raise RuntimeError("Arabic Sort response did not contain a URL.")
    if not session_id or not picard_token:
        raise RuntimeError(
            "Arabic Sort is still using the old one-way Picard endpoint."
        )

    return {
        "url": session_url,
        "session_id": session_id,
        "picard_token": picard_token,
        "expires_at": result.get("expires_at"),
    }


def _send_ack(session_id, token, command_id, payload):
    try:
        _request_json(
            API_URL
            + "/picard/control/%s/commands/%s/ack"
            % (session_id, command_id),
            method="POST",
            payload=payload,
            token=token,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        log.error(
            "%s: could not acknowledge command %s: %s",
            PLUGIN_NAME,
            command_id,
            exc,
        )


def _acknowledge(
    session_id,
    token,
    command_id,
    status,
    effective_title="",
    error="",
):
    if not session_id or not token:
        return

    payload = {
        "status": status,
        "effective_title": effective_title,
        "error": error,
    }
    thread = threading.Thread(
        target=_send_ack,
        args=(session_id, token, command_id, payload),
        daemon=True,
        name="ArabicSortAck",
    )
    thread.start()


def _refresh_picard(obj):
    try:
        obj.tagger.window.refresh_metadatabox()
    except Exception:
        pass


def _apply_command_in_picard(command):
    command_id = command.get("command_id")
    row_id = str(command.get("row_id") or "")
    title = str(command.get("title") or "").strip()
    session_id = str(command.get("session_id") or "")
    control_token = str(command.get("picard_token") or "")

    try:
        if not title:
            raise RuntimeError("Arabic title is empty.")

        with _ACTIVE_LOCK:
            session = _ACTIVE_SESSION
            if session is None or session["session_id"] != session_id:
                raise RuntimeError("This Arabic Sort session is no longer active.")
            target = session["targets"].get(row_id)

        if target is None:
            raise RuntimeError("The corresponding Picard track no longer exists.")

        obj = target["object"]
        kind = target["kind"]

        if kind == "file":
            obj.metadata["title"] = title
            obj.update()
            _refresh_picard(obj)

        elif kind == "track":
            linked_files = list(getattr(obj, "files", []) or [])
            if not linked_files:
                raise RuntimeError("The Picard track has no linked audio file.")

            obj.metadata["title"] = title
            for file_obj in linked_files:
                obj.update_file_metadata(file_obj)
            try:
                obj.update()
            except Exception:
                pass
            _refresh_picard(obj)

        else:
            raise RuntimeError("Unknown Picard target type.")

        log.info(
            "%s: applied command %s to one %s title",
            PLUGIN_NAME,
            command_id,
            kind,
        )
        _acknowledge(
            session_id,
            control_token,
            command_id,
            "applied",
            effective_title=title,
        )

    except Exception as exc:
        log.error(
            "%s: failed to apply command %s: %s",
            PLUGIN_NAME,
            command_id,
            exc,
        )
        _acknowledge(
            session_id,
            control_token,
            command_id,
            "failed",
            error=str(exc),
        )


_BRIDGE_SIGNALS.command_received.connect(
    _apply_command_in_picard,
    QtCore.Qt.QueuedConnection,
)


def _poll_worker(session_id, token, stop_event):
    after = 0
    while not stop_event.is_set():
        with _ACTIVE_LOCK:
            session = _ACTIVE_SESSION
            if session is None or session["session_id"] != session_id:
                return

        try:
            result = _request_json(
                API_URL
                + "/picard/control/%s/commands?after=%s"
                % (session_id, after),
                token=token,
                timeout=POLL_TIMEOUT_SECONDS,
            )
            for command in result.get("commands", []):
                try:
                    command_id = int(command.get("command_id"))
                except (TypeError, ValueError):
                    continue
                after = max(after, command_id)
                command["session_id"] = session_id
                command["picard_token"] = token
                _BRIDGE_SIGNALS.command_received.emit(command)
        except Exception as exc:
            if "HTTP 404" in str(exc) or "HTTP 401" in str(exc):
                log.error("%s: polling stopped: %s", PLUGIN_NAME, exc)
                return
            log.error("%s: polling retry after error: %s", PLUGIN_NAME, exc)

        stop_event.wait(POLL_INTERVAL_SECONDS)


def _activate_session(session_info, targets):
    global _ACTIVE_SESSION

    with _ACTIVE_LOCK:
        previous = _ACTIVE_SESSION
        if previous is not None:
            previous["stop_event"].set()

        stop_event = threading.Event()
        _ACTIVE_SESSION = {
            "session_id": session_info["session_id"],
            "picard_token": session_info["picard_token"],
            "targets": targets,
            "stop_event": stop_event,
        }

    thread = threading.Thread(
        target=_poll_worker,
        args=(
            session_info["session_id"],
            session_info["picard_token"],
            stop_event,
        ),
        daemon=True,
        name="ArabicSortPoll",
    )
    thread.start()


def _show_error(message):
    log.error("%s: %s", PLUGIN_NAME, message)
    QtWidgets.QMessageBox.critical(None, PLUGIN_NAME, str(message))


def _present_session_url(session_url, payload):
    clipboard = QtWidgets.QApplication.clipboard()
    clipboard.setText(session_url)
    opened = QtGui.QDesktopServices.openUrl(QtCore.QUrl(session_url))

    message = (
        "Arabic Sort session created for:\n\n"
        "%s\n%s\n%d track(s)\n\n"
        "The link has been copied to the shared clipboard:\n\n%s"
        % (
            payload.get("artist") or "(artist not set)",
            payload.get("album") or "(album not set)",
            len(payload.get("titles") or []),
            session_url,
        )
    )

    if opened:
        message += (
            "\n\nPicard also asked the container desktop to open the link. "
            "If no page appears in your outer browser, open a new tab and paste."
        )
    else:
        message += "\n\nOpen a new browser tab and paste the copied link."

    message += (
        "\n\nKeep this Picard session open while using the per-track Send buttons."
    )
    QtWidgets.QMessageBox.information(None, PLUGIN_NAME, message)


class ArabicTaggingAction(BaseAction):
    NAME = "Arabic tagging…"
    MENU = ["Arabic Sort"]

    def callback(self, objs):
        selected = list(objs)
        if len(selected) != 1:
            _show_error("Select exactly one cluster or album.")
            return

        try:
            payload, targets = _extract_payload_and_targets(selected[0])
            session_info = _create_session(payload)
            _activate_session(session_info, targets)
            _present_session_url(session_info["url"], payload)
        except Exception as exc:
            _show_error(exc)


action = ArabicTaggingAction()
register_cluster_action(action)
register_album_action(action)
