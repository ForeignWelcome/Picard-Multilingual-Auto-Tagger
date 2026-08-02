# -*- coding: utf-8 -*-

PLUGIN_NAME = "Arabic Sort Bridge"
PLUGIN_AUTHOR = "Custom local integration"
PLUGIN_DESCRIPTION = (
    "Sends one selected Picard cluster or album to the local Arabic Sort "
    "Bridge and creates a temporary prefilled review page."
)
PLUGIN_VERSION = "1.0.0"
PLUGIN_API_VERSIONS = ["2.2"]
PLUGIN_LICENSE = "GPL-2.0-or-later"
PLUGIN_LICENSE_URL = "https://www.gnu.org/licenses/gpl-2.0.html"

import json
import os
import re
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


def _number(value, default=999999):
    """Return the first integer found in a Picard number field."""
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


def _payload_from_cluster(cluster):
    files = _ordered_items(cluster.files)
    if not files:
        raise ValueError("The selected cluster contains no files.")

    artist = _metadata_value(
        cluster.metadata,
        "albumartist",
        "artist",
    )
    if not artist:
        artist = _metadata_value(
            files[0].metadata,
            "albumartist",
            "artist",
        )

    album = _metadata_value(cluster.metadata, "album")
    if not album:
        album = _metadata_value(files[0].metadata, "album")

    titles = []
    for position, file_obj in enumerate(files, start=1):
        title = _metadata_value(file_obj.metadata, "title")
        if not title:
            raise ValueError(
                "Track %d in the selected cluster has no title tag." % position
            )
        titles.append(title)

    return {
        "artist": artist,
        "album": album,
        "titles": titles,
        "source": "cluster",
    }


def _payload_from_album(album_obj):
    tracks = _ordered_items(album_obj.tracks)
    if not tracks:
        raise ValueError("The selected MusicBrainz album contains no tracks.")

    artist = _metadata_value(
        album_obj.metadata,
        "albumartist",
        "artist",
    )
    album = _metadata_value(album_obj.metadata, "album")

    titles = []
    for position, track in enumerate(tracks, start=1):
        title = _metadata_value(track.metadata, "title")
        if not title:
            raise ValueError(
                "Track %d in the selected album has no title." % position
            )
        titles.append(title)

    return {
        "artist": artist,
        "album": album,
        "titles": titles,
        "source": "album",
    }


def _extract_payload(obj):
    if isinstance(obj, Cluster):
        return _payload_from_cluster(obj)
    if isinstance(obj, Album):
        return _payload_from_album(obj)
    raise ValueError("Select one Picard cluster or one MusicBrainz album.")


def _create_session(payload):
    request = Request(
        API_URL + "/picard/prefill",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            "Arabic Sort returned HTTP %s:\n%s" % (exc.code, body)
        )
    except URLError as exc:
        raise RuntimeError(
            "Could not connect to Arabic Sort at %s.\n\n%s"
            % (API_URL, exc.reason)
        )

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("Arabic Sort returned invalid JSON:\n%s" % raw)

    if not result.get("ok"):
        raise RuntimeError(
            "Arabic Sort did not create a session:\n%s"
            % result.get("detail", result)
        )

    path = str(result.get("path") or "").strip()
    if path:
        return urljoin(PUBLIC_URL + "/", path.lstrip("/"))

    session_url = str(result.get("url") or "").strip()
    if not session_url:
        raise RuntimeError("Arabic Sort response did not contain a URL.")
    return session_url


def _show_error(message):
    log.error("%s: %s", PLUGIN_NAME, message)
    QtWidgets.QMessageBox.critical(
        None,
        PLUGIN_NAME,
        str(message),
    )


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

    QtWidgets.QMessageBox.information(
        None,
        PLUGIN_NAME,
        message,
    )


class ArabicTaggingAction(BaseAction):
    NAME = "Arabic tagging…"
    MENU = ["Arabic Sort"]

    def callback(self, objs):
        selected = list(objs)

        if len(selected) != 1:
            _show_error("Select exactly one cluster or album.")
            return

        try:
            payload = _extract_payload(selected[0])
            session_url = _create_session(payload)
            _present_session_url(session_url, payload)
        except Exception as exc:
            _show_error(exc)


action = ArabicTaggingAction()
register_cluster_action(action)
register_album_action(action)
