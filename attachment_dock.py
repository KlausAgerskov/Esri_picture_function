# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Naturstyrelsen
# SPDX-License-Identifier: GPL-2.0-or-later
"""
attachment_dock.py
------------------
A dock widget that shows the attachments of the features selected in the
active ArcGIS Feature Service layer.

All network traffic goes through the QGIS authentication manager, so OAuth2
against an ArcGIS Enterprise portal works without handling tokens by hand.

Attachments may hang on the layer itself or on a related table; both are
discovered from the service metadata.

Large selections are fetched in batches of BATCH_SIZE features. The first
batch starts by itself, the rest on request, with a warning before a very
large fetch. Features whose pictures are in the panel are marked on the map,
and clicking a picture points out the feature it belongs to. With "Keep"
ticked (the default) the panel survives deselection and new selections are
added to it. The features in the panel can be exported for a print layout.
"""

import hashlib
import json
import os
import re
import tempfile
import time
from urllib.parse import urlencode

from qgis.PyQt.QtCore import Qt, QUrl, QSize, QTimer, pyqtSignal
from qgis.PyQt.QtGui import (
    QColor, QPixmap, QIcon, QImageReader, QTransform, QDesktopServices,
)
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QCheckBox, QListWidget, QListWidgetItem, QAbstractItemView,
    QSizePolicy, QFileDialog, QListView, QMenu, QMessageBox,
)

from qgis.core import (
    Qgis, QgsApplication, QgsBlockingNetworkRequest, QgsDataSourceUri,
    QgsFeatureRequest, QgsMessageLog, QgsProject, QgsTask,
)
from qgis.gui import (
    QgsAttributeDialog, QgsAttributeEditorContext, QgsHighlight,
)

LOG_TAG = "Esri Pictures"

#: Features per fetch. Pictures appear batch by batch, so a big selection
#: shows something quickly and can be stopped half way. 25 object ids also
#: keep the queryRelatedRecords URL well below the server's length limit.
BATCH_SIZE = 25

#: Kept for callers that still import the old name
MAX_FEATURES = BATCH_SIZE

#: Ask before fetching more features than this in one go
WARN_FEATURES = 100

#: Field names tried as the object id, in order of preference
OID_CANDIDATES = ("OBJECTID", "objectid", "ObjectID", "OID", "FID")

IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/bmp", "image/webp")

#: Edge length of the thumbnails in the list
THUMB_SIZE = 128

#: Item data roles: the cached file, and the parent object id of a picture
ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_PARENT = int(Qt.ItemDataRole.UserRole) + 1

#: Map marking: features with pictures, features without, the active picture
COLOR_LOADED = QColor(0, 160, 80)
COLOR_EMPTY = QColor(150, 150, 150)
COLOR_ACTIVE = QColor(255, 190, 0)

#: The cache is pruned at start-up: everything older than this first, then the
#: oldest files until the total size is below the limit.
CACHE_MAX_AGE_DAYS = 14
CACHE_MAX_BYTES = 500 * 1024 * 1024


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

def make_thumbnail(path, box=THUMB_SIZE):
    """Read an image as a thumbnail.

    QImageReader.setScaledSize() lets the JPEG decoder skip pixels while
    reading instead of expanding a 12 MP photo in memory and scaling it
    afterwards — considerably faster than QPixmap(path).scaled().

    EXIF orientation is deliberately NOT applied: setAutoTransform() disables
    that fast decoding path. Photos with a rotation tag therefore appear in
    their stored orientation and can be turned with the rotate button. Images
    written by the layout export are already rotated.

    Returns a QImage, not a QPixmap. QPixmap may only be touched on the GUI
    thread, and this function is called from a background task.
    """
    reader = QImageReader(path)

    size = reader.size()
    if size.isValid() and (size.width() > box or size.height() > box):
        reader.setScaledSize(size.scaled(box, box, Qt.AspectRatioMode.KeepAspectRatio))

    image = reader.read()
    if image.isNull():
        return None
    if image.width() > box or image.height() > box:
        image = image.scaled(box, box, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return image


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def prune_cache(cache_dir, max_age_days=CACHE_MAX_AGE_DAYS,
                max_bytes=CACHE_MAX_BYTES):
    """Clean the image cache. Returns (files removed, bytes freed).

    Age is judged by mtime rather than atime: NTFS does not update last-access
    times on large volumes by default, which would make atime useless on
    Windows. Instead mtime is pushed forward on every cache hit, so a file
    still in use is not evicted.
    """
    if not os.path.isdir(cache_dir):
        return 0, 0

    cutoff = time.time() - max_age_days * 86400
    entries = []
    removed = 0
    freed = 0

    for entry in os.scandir(cache_dir):
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue

        if stat.st_mtime < cutoff:
            try:
                os.remove(entry.path)
                removed += 1
                freed += stat.st_size
            except OSError:
                pass
        else:
            entries.append((stat.st_mtime, stat.st_size, entry.path))

    total = sum(size for _mtime, size, _path in entries)
    if total > max_bytes:
        entries.sort()  # oldest first
        for _mtime, size, path in entries:
            if total <= max_bytes:
                break
            try:
                os.remove(path)
                total -= size
                removed += 1
                freed += size
            except OSError:
                pass

    return removed, freed


def cache_size(cache_dir):
    """Total size of the cache in bytes."""
    if not os.path.isdir(cache_dir):
        return 0
    total = 0
    for entry in os.scandir(cache_dir):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            pass
    return total


def cache_path(cache_dir, url, name):
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    safe = re.sub(r"[^\w.\-]", "_", name or "attachment")
    return os.path.join(cache_dir, "{}_{}".format(digest, safe))


# --------------------------------------------------------------------------
# Layer helpers
# --------------------------------------------------------------------------

def parse_afs_source(layer):
    """(service_url, authcfg) for an ArcGIS REST layer, else (None, None)."""
    if layer is None or layer.providerType() not in ("arcgisfeatureserver", "afs"):
        return None, None

    source = layer.source()
    try:
        uri = QgsDataSourceUri(source)
        url = uri.param("url")
        if url:
            return url, (uri.authConfigId() or None)
    except Exception:  # noqa: BLE001 — unexpected source syntax
        pass

    m_url = re.search(r"url='([^']+)'", source)
    m_auth = re.search(r"authcfg=([A-Za-z0-9_]+)", source)
    if m_url:
        return m_url.group(1), (m_auth.group(1) if m_auth else None)
    return None, None


def split_service_url(url):
    """'.../FeatureServer/3' -> ('.../FeatureServer', 3)."""
    url = url.rstrip("/")
    match = re.match(r"^(.*/(?:Feature|Map)Server)/(\d+)$", url, re.IGNORECASE)
    if match:
        return match.group(1), int(match.group(2))
    return url, None


def normalize_globalid(value):
    """ArcGIS expects GlobalIDs in braces and upper case."""
    text = str(value).strip().upper()
    if not text.startswith("{"):
        text = "{" + text
    if not text.endswith("}"):
        text = text + "}"
    return text


def oid_field_name(layer):
    """Name of the object id field, or None."""
    names = [f.name() for f in layer.fields()]
    return next((c for c in OID_CANDIDATES if c in names), None)


def feature_oid(layer, feature, oid_field=None):
    """Object id of one feature.

    The internal QGIS feature id does not always equal OBJECTID on ArcGIS
    layers, so the field is read when it exists.
    """
    if oid_field is None:
        oid_field = oid_field_name(layer)
    if oid_field is not None:
        value = feature[oid_field]
        if value is not None:
            return int(value)
    return int(feature.id())


def selected_oids(layer):
    """{object id: QGIS feature id} for the selected features, by object id.

    The server understands the object id; flashing, zooming and highlighting
    on the canvas need the QGIS feature id. The two are not always equal, so
    both are kept. Sorting makes the batches the same from run to run.
    """
    oid_field = oid_field_name(layer)
    request = QgsFeatureRequest().setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    if oid_field is not None:
        request.setSubsetOfAttributes([oid_field], layer.fields())
    pairs = [(feature_oid(layer, f, oid_field), f.id())
             for f in layer.getSelectedFeatures(request)]
    return dict(sorted(pairs))


def object_ids(layer, limit=None):
    """Object ids of the selected features, sorted, optionally capped."""
    oids = list(selected_oids(layer))
    return oids[:limit] if limit else oids


def global_ids(layer, field="GLOBALID", limit=MAX_FEATURES):
    """Normalized GlobalIDs of the selected features."""
    names = [f.name() for f in layer.fields()]
    actual = next((n for n in names if n.upper() == field.upper()), None)
    if actual is None:
        return []

    out = []
    for feat in layer.selectedFeatures():
        if len(out) >= limit:
            break
        value = feat[actual]
        if value:
            out.append(normalize_globalid(value))
    return out


# --------------------------------------------------------------------------
# REST client
# --------------------------------------------------------------------------

class RestClient(object):
    """Thin wrapper around QgsBlockingNetworkRequest.

    Only ever used inside QgsTask.run(), never on the main thread — it blocks
    the event loop of whichever thread it runs on.
    """

    def __init__(self, authcfg=None):
        self.authcfg = authcfg

    def get(self, url):
        request = QgsBlockingNetworkRequest()
        if self.authcfg:
            request.setAuthCfg(self.authcfg)
        code = request.get(QNetworkRequest(QUrl(url)))
        if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
            raise RuntimeError(request.errorMessage() or "network error")
        return bytes(request.reply().content())

    def get_json(self, url, **params):
        if params:
            url = "{}{}{}".format(url, "&" if "?" in url else "?", urlencode(params))
        payload = json.loads(self.get(url).decode("utf-8"))
        # ArcGIS answers 200 OK with an error body — catch that explicitly.
        if isinstance(payload, dict) and "error" in payload:
            err = payload["error"]
            raise RuntimeError("{}: {}".format(
                err.get("code", "?"), err.get("message", "unknown error")))
        return payload


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

class AttachmentSource(object):
    """Where the pictures come from.

    relationship_id is None when the attachments hang on the selected layer
    itself, and otherwise the id of the relationship leading to the table that
    carries them.
    """

    def __init__(self, label, target_url, target_oid_field="OBJECTID",
                 relationship_id=None):
        self.label = label
        self.target_url = target_url.rstrip("/")
        self.target_oid_field = target_oid_field
        self.relationship_id = relationship_id


class DiscoverTask(QgsTask):
    """Read the service metadata and work out where attachments live."""

    ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, layer_url, authcfg):
        super().__init__("Inspecting service", QgsTask.Flag.CanCancel)
        self._url = layer_url.rstrip("/")
        self._client = RestClient(authcfg)
        self._sources = []
        self._error = ""

    def run(self):
        try:
            base, _layer_id = split_service_url(self._url)
            meta = self._client.get_json(self._url, f="json")

            if meta.get("hasAttachments"):
                self._sources.append(AttachmentSource(
                    "This layer",
                    self._url,
                    meta.get("objectIdField", "OBJECTID"),
                ))

            if base is None:
                return True

            for rel in meta.get("relationships", []):
                # Only relationships where our layer is the origin; the
                # destination side points the wrong way, and many-to-many
                # goes through an intermediate table.
                if rel.get("role") not in (None, "esriRelRoleOrigin"):
                    continue

                related_id = rel.get("relatedTableId")
                if related_id is None:
                    continue

                child_url = "{}/{}".format(base, related_id)
                child = self._client.get_json(child_url, f="json")
                if not child.get("hasAttachments"):
                    continue

                self._sources.append(AttachmentSource(
                    rel.get("name") or child.get("name") or "Related table",
                    child_url,
                    child.get("objectIdField", "OBJECTID"),
                    relationship_id=rel.get("id"),
                ))

            return True

        except Exception as exc:  # noqa: BLE001 — reported to the user
            self._error = str(exc)
            return False

    def finished(self, result):
        if result:
            self.ready.emit(self._sources)
        elif not self.isCanceled():
            QgsMessageLog.logMessage(self._error, LOG_TAG, Qgis.MessageLevel.Warning)
            self.failed.emit(self._error)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class AttachmentTask(QgsTask):
    """Fetch metadata and binary data for one batch of parent features.

    Every result carries both the object id the attachment hangs on ("oid")
    and the object id of the feature in the layer ("parent_oid"). They differ
    when the attachments sit on a related table, and the panel needs the
    parent to point out the feature on the map.
    """

    ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, layer_url, authcfg, source, parent_oids, cache_dir):
        super().__init__("Fetching attachments", QgsTask.Flag.CanCancel)
        self._layer_url = layer_url.rstrip("/")
        self._client = RestClient(authcfg)
        self._source = source
        self._parent_oids = list(parent_oids)
        self._cache_dir = cache_dir
        self._results = []
        self._error = ""

    # -- lookups ----------------------------------------------------------

    def _related_oids(self):
        """[(child object id, parent object id)] via queryRelatedRecords.

        A single call covers the whole batch. The key field does not matter
        here — the REST layer resolves the relationship itself, whether it is
        built on OBJECTID or on GlobalID.
        """
        payload = self._client.get_json(
            "{}/queryRelatedRecords".format(self._layer_url),
            objectIds=",".join(str(o) for o in self._parent_oids),
            relationshipId=self._source.relationship_id,
            outFields=self._source.target_oid_field,
            returnGeometry="false",
            f="json",
        )

        oid_field = payload.get("objectIdFieldName") or self._source.target_oid_field
        out = []
        for group in payload.get("relatedRecordGroups", []):
            parent = group.get("objectId")
            if parent is None:
                continue
            for record in group.get("relatedRecords", []):
                value = record.get("attributes", {}).get(oid_field)
                if value is not None:
                    out.append((int(value), int(parent)))
        return out

    def _query_attachments(self, oids):
        payload = self._client.get_json(
            "{}/queryAttachments".format(self._source.target_url),
            objectIds=",".join(str(o) for o in oids),
            returnUrl="false",
            f="json",
        )
        pairs = []
        for group in payload.get("attachmentGroups", []):
            parent_oid = group.get("parentObjectId")
            if parent_oid is None:
                continue
            for info in group.get("attachmentInfos", []):
                pairs.append((int(parent_oid), info))
        return pairs

    def _per_feature_attachments(self, oids):
        pairs = []
        for oid in oids:
            if self.isCanceled():
                break
            payload = self._client.get_json(
                "{}/{}/attachments".format(self._source.target_url, oid), f="json")
            for info in payload.get("attachmentInfos", []):
                pairs.append((int(oid), info))
        return pairs

    def _metadata(self):
        """[(child object id, parent object id, attachmentInfo)].

        Sorted by parent, child and attachment id — the same order as the
        layout export, so picture 1 in the panel is picture 1 in the layout.
        """
        if self._source.relationship_id is None:
            links = [(oid, oid) for oid in self._parent_oids]
        else:
            links = self._related_oids()

        if not links:
            return []

        parent_of = {}
        for child, parent in links:
            parent_of.setdefault(child, parent)
        children = sorted(parent_of)

        try:
            pairs = self._query_attachments(children)
        except RuntimeError:
            # The service lacks supportsQueryAttachments — one call per feature.
            pairs = self._per_feature_attachments(children)

        triples = [(child, parent_of.get(child, child), info)
                   for child, info in pairs]
        triples.sort(key=lambda t: (t[1], t[0], t[2].get("id", 0)))
        return triples

    # -- QgsTask ----------------------------------------------------------

    def run(self):
        try:
            triples = self._metadata()
            total = max(len(triples), 1)

            for index, (oid, parent_oid, info) in enumerate(triples):
                if self.isCanceled():
                    return False

                url = "{}/{}/attachments/{}".format(
                    self._source.target_url, oid, info["id"])
                path = cache_path(self._cache_dir, url, info.get("name"))

                if not os.path.exists(path):
                    data = self._client.get(url)
                    with open(path, "wb") as handle:
                        handle.write(data)
                else:
                    # Mark as still in use so the pruner leaves it alone.
                    try:
                        os.utime(path, None)
                    except OSError:
                        pass

                content_type = info.get("contentType", "")
                thumb = None
                if content_type in IMAGE_TYPES:
                    try:
                        thumb = make_thumbnail(path)
                    except Exception:  # noqa: BLE001 — one bad image
                        thumb = None   # must not sink the whole fetch

                self._results.append({
                    "oid": oid,
                    "parent_oid": parent_oid,
                    "id": info.get("id"),
                    "name": info.get("name", ""),
                    "content_type": content_type,
                    "size": info.get("size", 0),
                    "path": path,
                    "thumb": thumb,
                })
                self.setProgress(100.0 * (index + 1) / total)
            return True

        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            return False

    def finished(self, result):
        """Always called on the main thread — safe to touch the GUI here."""
        if result:
            self.ready.emit(self._results)
        elif not self.isCanceled():
            QgsMessageLog.logMessage(self._error, LOG_TAG, Qgis.MessageLevel.Warning)
            self.failed.emit(self._error)


# --------------------------------------------------------------------------
# Dock widget
# --------------------------------------------------------------------------

class AttachmentDock(QDockWidget):
    """The picture panel.

    The panel holds a set of parent features, each in one of three states:

    * waiting  — in self._queue, not fetched yet
    * fetching — in self._batch, the running task works on it
    * loaded   — in self._loaded, with the number of pictures it got

    A new selection either replaces the set or, with Keep ticked, is added to
    it. Only one batch runs at a time.
    """

    #: The Export button was pressed. The plugin does the export, because it
    #: owns the message bar and the expression functions.
    export_requested = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__("Attachments", parent)
        self.setObjectName("esriPictureAttachmentDock")
        self.iface = iface

        self._layer = None
        self._layer_url = None
        self._authcfg = None
        self._sources = []
        self._source_cache = {}      # layer_url -> [AttachmentSource]
        self._task = None
        self._discover = None
        self._form = None
        self._form_feature = None    # keeps the feature alive for the dialog
        self._form_geometry = None   # remember where the window was

        self._fids = {}              # parent oid -> QGIS feature id
        self._queue = []             # parent oids waiting
        self._batch = []             # parent oids being fetched
        self._loaded = {}            # parent oid -> number of pictures
        self._auto_continue = False  # fetch the whole queue without asking
        self._highlights = {}        # parent oid -> QgsHighlight
        self._active = None          # highlight of the clicked picture
        self._suppress_selection = False

        self._cache_dir = os.path.join(tempfile.gettempdir(), "esri_attachments")
        os.makedirs(self._cache_dir, exist_ok=True)

        removed, freed = prune_cache(self._cache_dir)
        if removed:
            QgsMessageLog.logMessage(
                "Cache: removed {} file(s), freed {:.1f} MB".format(
                    removed, freed / 1048576.0), LOG_TAG, Qgis.MessageLevel.Info)

        # The selection signal fires repeatedly during a rubber band
        # selection; collect those into one reload.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._reload)

        self._build_ui()

        self.visibilityChanged.connect(self._visibility_changed)
        # The service descriptions are remembered for the session; reopening
        # the project is how a changed service is picked up.
        QgsProject.instance().cleared.connect(self._forget_sources)
        iface.layerTreeView().currentLayerChanged.connect(self._layer_changed)
        self._bind_layer(iface.activeLayer())

    # -- construction -----------------------------------------------------

    def _build_ui(self):
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self.source_box = QComboBox()
        self.source_box.setToolTip("Where the pictures are fetched from")
        self.source_box.currentIndexChanged.connect(self._source_changed)
        layout.addWidget(self.source_box)

        self.status = QLabel("Select an ArcGIS layer.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # Shown while features are waiting to be fetched
        self.batch_bar = QWidget()
        bar = QHBoxLayout(self.batch_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        self.batch_label = QLabel()
        self.batch_label.setWordWrap(True)
        bar.addWidget(self.batch_label, 1)
        self.next_button = QPushButton()
        self.next_button.setToolTip("Fetch the next batch of features")
        self.next_button.clicked.connect(self._fetch_next)
        bar.addWidget(self.next_button)
        self.all_button = QPushButton()
        self.all_button.setToolTip(
            "Fetch every waiting feature, one batch after the other")
        self.all_button.clicked.connect(self._fetch_all)
        bar.addWidget(self.all_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip(
            "Stop fetching; features not yet fetched stay waiting")
        self.stop_button.clicked.connect(self._stop)
        bar.addWidget(self.stop_button)
        self.batch_bar.hide()
        layout.addWidget(self.batch_bar)

        self.list = QListWidget()
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.list.setGridSize(QSize(THUMB_SIZE + 16, THUMB_SIZE + 36))
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setWordWrap(True)
        self.list.itemDoubleClicked.connect(self._open_item)
        self.list.currentItemChanged.connect(self._item_changed)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.list)

        # -- first row: how the panel follows the map, and the export
        row1 = QHBoxLayout()

        self.pin_check = QCheckBox("Keep")
        self.pin_check.setChecked(True)
        self.pin_check.setToolTip(
            "Keep the pictures when the selection changes. New selections are "
            "added to the panel, and the panel stays with its layer. Use "
            "Clear panel to start over.")
        self.pin_check.toggled.connect(self._pin_toggled)
        row1.addWidget(self.pin_check)

        self.mark_check = QCheckBox("Mark on map")
        self.mark_check.setChecked(True)
        self.mark_check.setToolTip(
            "Green: features whose pictures are in the panel. Grey: fetched, "
            "but without pictures. Yellow: the picture clicked in the panel.")
        self.mark_check.toggled.connect(self._mark_toggled)
        row1.addWidget(self.mark_check)
        row1.addStretch()

        self.export_button = QPushButton()
        self.export_button.setToolTip(
            "Save the pictures of every feature in the panel to a folder, "
            "rotated and size-capped, for use in a print layout with atlas")
        self.export_button.clicked.connect(self.export_requested)
        row1.addWidget(self.export_button)
        layout.addLayout(row1)

        # -- second row: the panel and the cache
        row2 = QHBoxLayout()

        # Only shown when inspecting the service failed — an expired login
        # or a network hiccup would otherwise leave the panel stuck on the
        # error until the user switched layers back and forth.
        self.retry_button = QPushButton("Try again")
        self.retry_button.setToolTip("Inspect the service again")
        self.retry_button.clicked.connect(self._rediscover)
        self.retry_button.hide()
        row2.addWidget(self.retry_button)

        clear_panel = QPushButton("Clear panel")
        clear_panel.setToolTip("Remove every picture and mark from the panel")
        clear_panel.clicked.connect(self._clear_panel)
        row2.addWidget(clear_panel)

        self.clear_button = QPushButton()
        self.clear_button.clicked.connect(self._clear_cache)
        row2.addWidget(self.clear_button)
        row2.addStretch()
        layout.addLayout(row2)

        self._update_cache_button()
        self._update_export_button()
        self.setWidget(container)

    # -- public state -----------------------------------------------------
    #
    # The layout export needs what this panel has already worked out.
    # Properties rather than direct access to the _fields, so the internals
    # can change without breaking callers.

    @property
    def layer(self):
        """The panel's layer if it is an ArcGIS layer, else None."""
        return self._layer if self._layer_url else None

    @property
    def service_url(self):
        """URL of the layer's FeatureServer layer, or None."""
        return self._layer_url

    @property
    def authcfg(self):
        """The layer's authcfg id, or None for an open service."""
        return self._authcfg

    @property
    def sources(self):
        """Attachment sources discovered for the layer."""
        return list(self._sources)

    @property
    def current_source(self):
        """The source picked in the combo box, or None."""
        if not self._sources:
            return None
        index = max(self.source_box.currentIndex(), 0)
        return self._sources[index] if index < len(self._sources) else None

    @property
    def panel_oids(self):
        """Object ids of every feature in the panel — fetched, being
        fetched or waiting — sorted. This is what the layout export takes."""
        return sorted(set(self._loaded) | set(self._batch) | set(self._queue))

    @property
    def loaded_oids(self):
        """Object ids of the features whose pictures are in the panel."""
        return [oid for oid, count in self._loaded.items() if count]

    # -- layer and selection ----------------------------------------------

    def _has_content(self):
        return bool(self._loaded or self._queue or self._batch)

    def _layer_changed(self, layer):
        """The active layer changed in the layer tree."""
        if (self.pin_check.isChecked() and self._layer is not None
                and self._has_content()):
            # Kept: the panel stays with its layer until Keep is unticked,
            # so clicking around the layer tree does not throw it away.
            return
        self._bind_layer(layer)

    def _bind_layer(self, layer):
        self._reset()

        if self._layer is not None:
            try:
                self._layer.selectionChanged.disconnect(self._selection_changed)
                self._layer.willBeDeleted.disconnect(self._layer_deleted)
            except (TypeError, RuntimeError):
                pass  # the layer may already be gone

        self._layer = layer if (layer and hasattr(layer, "selectionChanged")) else None
        if self._layer is not None:
            self._layer.selectionChanged.connect(self._selection_changed)
            self._layer.willBeDeleted.connect(self._layer_deleted)

        self._layer_url, self._authcfg = parse_afs_source(self._layer)

        # The form belongs to the previous layer.
        self._close_form()

        self._load_sources()

    def _layer_deleted(self):
        """The panel's layer is being removed from the project.

        The highlights point at the layer and must go before it does.
        """
        self._reset()
        self._layer = None
        self._layer_url = None
        self._authcfg = None
        self._sources = []
        self.source_box.blockSignals(True)
        self.source_box.clear()
        self.source_box.blockSignals(False)
        self.retry_button.hide()
        self.status.setText("The layer was removed.")

    def _selection_changed(self, *_args):
        if self._suppress_selection:
            return
        self._debounce.start()

    # -- sources ----------------------------------------------------------

    def _load_sources(self, force=False):
        self._cancel_discover()
        self._reset()
        self.retry_button.hide()

        self.source_box.blockSignals(True)
        self.source_box.clear()
        self.source_box.blockSignals(False)

        if self._layer_url is None:
            self.status.setText("The active layer is not an ArcGIS Feature "
                                "Service layer.")
            return

        if not force and self._layer_url in self._source_cache:
            self._sources_ready(self._source_cache[self._layer_url])
            return

        self.status.setText("Inspecting service…")
        task = DiscoverTask(self._layer_url, self._authcfg)
        task.ready.connect(self._sources_ready)
        task.failed.connect(self._show_error)
        self._discover = task
        QgsApplication.taskManager().addTask(task)

    def _sources_ready(self, sources):
        self._discover = None
        self._sources = sources
        if self._layer_url:
            self._source_cache[self._layer_url] = sources

        self.source_box.blockSignals(True)
        self.source_box.clear()
        for source in sources:
            self.source_box.addItem(source.label)
        self.source_box.setVisible(len(sources) > 1)
        self.source_box.blockSignals(False)

        if not sources:
            self.status.setText("Neither the layer nor its related tables "
                                "have attachments.")
            return

        self._reload()

    def _source_changed(self, *_args):
        """Another attachment source: the pictures in the panel are stale."""
        self._reset()
        self._reload()

    def _forget_sources(self):
        self._source_cache = {}

    def _rediscover(self):
        self._load_sources(force=True)

    def _cancel_discover(self):
        if self._discover is not None:
            try:
                self._discover.cancel()
            except RuntimeError:
                pass
            self._discover = None

    # -- the feature set --------------------------------------------------

    def _reload(self):
        """Follow the layer's selection (debounced)."""
        if self._layer is None or self._layer_url is None or not self._sources:
            return

        selected = selected_oids(self._layer)

        if self.pin_check.isChecked():
            # Kept: a new selection is added, a deselection changes nothing.
            if selected:
                self._enqueue(selected)
            return

        self._reset()
        if not selected:
            self.status.setText("No features selected.")
            return
        self._enqueue(selected)

    def _enqueue(self, selected):
        """Add {object id: feature id} to the set and start fetching.

        New features go to the front of the queue — what was just selected is
        what the user wants to see now.
        """
        self._fids.update(selected)
        known = set(self._loaded) | set(self._batch) | set(self._queue)
        new = [oid for oid in selected if oid not in known]
        self._queue[:0] = new

        if self._task is None and self._queue:
            # The first batch starts by itself; the rest waits for the
            # Next / All buttons unless an "All" run is already going.
            self._next_batch()
        else:
            self._update_batch_bar()
            self._update_status()

    def _next_batch(self):
        if self._task is not None or not self._queue:
            self._update_batch_bar()
            return

        source = self.current_source
        if source is None or self._layer_url is None:
            return

        batch = self._queue[:BATCH_SIZE]
        del self._queue[:BATCH_SIZE]
        self._batch = batch

        task = AttachmentTask(
            self._layer_url, self._authcfg, source, batch, self._cache_dir)
        # The task travels with the signal, so the answer of a task that was
        # cancelled — or replaced — after it had already finished is ignored.
        task.ready.connect(lambda items, t=task: self._batch_ready(t, items))
        task.failed.connect(lambda message, t=task: self._batch_failed(t, message))
        self._task = task
        # The task manager takes ownership; the reference is only for cancel.
        QgsApplication.taskManager().addTask(task)

        self._update_batch_bar()
        self._update_status()

    def _batch_ready(self, task, items):
        if task is not self._task:
            return
        self._task = None
        done, self._batch = self._batch, []

        counts = dict.fromkeys(done, 0)
        for item in items:
            parent = item["parent_oid"]
            counts[parent] = counts.get(parent, 0) + 1
            self._add_item(item)
        self._loaded.update(counts)
        self._mark(done)

        if self._auto_continue and self._queue:
            self._next_batch()
        else:
            self._auto_continue = False

        self._update_batch_bar()
        self._update_status()
        self._update_cache_button()

    def _batch_failed(self, task, message):
        if task is not self._task:
            return
        self._task = None
        # Back in the queue, so Next tries the same features again
        self._queue[:0] = self._batch
        self._batch = []
        self._auto_continue = False
        self._update_batch_bar()
        self._update_status(error=message)

    def _fetch_next(self, *_args):
        self._auto_continue = False
        self._next_batch()

    def _fetch_all(self, *_args):
        waiting = len(self._queue)
        if not waiting:
            return

        if waiting > WARN_FEATURES:
            batches = (waiting + BATCH_SIZE - 1) // BATCH_SIZE
            answer = QMessageBox.question(
                self, "Fetch pictures",
                "You are about to fetch pictures for {} features in {} "
                "batches.\n\nThat can take a while and put a lot of data in "
                "the cache (limit {} MB, enforced at start-up). The fetch can "
                "be stopped at any time.\n\nContinue?".format(
                    waiting, batches, CACHE_MAX_BYTES // 1048576),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._auto_continue = True
        # When a batch is already running, the next one starts as it finishes.
        self._next_batch()
        self._update_batch_bar()

    def _stop(self, *_args):
        """Stop fetching. The running batch goes back to the queue."""
        self._auto_continue = False
        self._stop_task()
        self._queue[:0] = self._batch
        self._batch = []
        self._update_batch_bar()
        self._update_status()

    def _stop_task(self):
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
            self._task = None

    def _reset(self):
        """Empty the panel and forget every feature in it."""
        self._stop_task()
        self._auto_continue = False
        self._queue = []
        self._batch = []
        self._loaded = {}
        self._fids = {}
        self._clear_highlights()
        self.list.clear()
        self._update_batch_bar()

    def _clear_panel(self, *_args):
        self._reset()
        self.status.setText("Panel cleared.")

    # -- status -----------------------------------------------------------

    def _update_status(self, error=None):
        parts = []
        if error:
            parts.append("Error: {}".format(error))

        if self._task is not None:
            parts.append("Fetching {} feature(s)…".format(len(self._batch)))

        if self._loaded:
            with_pictures = sum(1 for count in self._loaded.values() if count)
            empty = len(self._loaded) - with_pictures
            text = "{} picture(s) from {} feature(s)".format(
                self.list.count(), with_pictures)
            if empty:
                text += ", {} without pictures".format(empty)
            parts.append(text + ".")
            if self.list.count():
                parts.append("Click a picture to find its feature, "
                             "double-click to open, right-click for more.")
        elif not parts:
            parts.append("No features selected.")

        self.status.setText(" ".join(parts))

    def _update_batch_bar(self):
        waiting = len(self._queue)
        running = self._task is not None
        total = len(self._loaded) + len(self._batch) + waiting

        self.batch_bar.setVisible(bool(waiting) or (running and self._auto_continue))
        self.batch_label.setText("{} of {} feature(s) fetched, {} waiting.".format(
            len(self._loaded), total, waiting))

        self.next_button.setText("Next {}".format(min(BATCH_SIZE, waiting) or BATCH_SIZE))
        self.next_button.setEnabled(bool(waiting) and not running)
        self.all_button.setText("All ({})".format(waiting))
        self.all_button.setEnabled(bool(waiting) and not self._auto_continue)
        self.stop_button.setVisible(running)
        self._update_export_button()

    def _update_export_button(self):
        count = len(self.panel_oids)
        self.export_button.setText(
            "Export for layout ({})…".format(count) if count
            else "Export for layout…")
        self.export_button.setEnabled(bool(count))

    def _show_error(self, message):
        """The service inspection failed. Nothing is cached, so a retry asks
        the server again."""
        self._discover = None
        self.status.setText("Error: {}".format(message))
        self.retry_button.setVisible(self._layer_url is not None)

    # -- list -------------------------------------------------------------

    def _add_item(self, item):
        parent = item["parent_oid"]
        entry = QListWidgetItem("{}\n(OID {})".format(item["name"], parent))
        entry.setData(ROLE_PATH, item["path"])
        entry.setData(ROLE_PARENT, parent)

        tooltip = "{}\n{}\n{:.0f} kB\nFeature OID {}".format(
            item["name"], item["content_type"], (item["size"] or 0) / 1024.0,
            parent)
        if item["oid"] != parent:
            tooltip += "\nRelated record OID {}".format(item["oid"])
        entry.setToolTip(tooltip)

        if item.get("thumb") is not None:
            entry.setIcon(QIcon(QPixmap.fromImage(item["thumb"])))
        else:
            entry.setIcon(QgsApplication.getThemeIcon("mActionFileNew.svg"))

        self.list.addItem(entry)

    def _fid_of(self, entry):
        """QGIS feature id of the feature a picture belongs to, or None."""
        if entry is None or self._layer is None:
            return None
        oid = entry.data(ROLE_PARENT)
        if oid is None:
            return None
        return self._fids.get(int(oid))

    def _feature(self, fid):
        if self._layer is None or fid is None:
            return None
        feature = self._layer.getFeature(fid)
        return feature if feature.isValid() else None

    # -- marking on the map -----------------------------------------------

    def _make_highlight(self, feature, color, width):
        highlight = QgsHighlight(self.iface.mapCanvas(), feature, self._layer)
        highlight.setColor(color)
        fill = QColor(color)
        fill.setAlpha(70)
        highlight.setFillColor(fill)
        highlight.setWidth(width)
        highlight.show()
        return highlight

    def _drop_highlight(self, highlight):
        try:
            self.iface.mapCanvas().scene().removeItem(highlight)
        except RuntimeError:
            pass

    def _mark(self, oids):
        """Mark the given features on the map: green with pictures, grey without."""
        if (not self.mark_check.isChecked() or self._layer is None
                or not self.isVisible()):
            return

        oid_of = {self._fids[oid]: oid for oid in oids if oid in self._fids}
        if not oid_of:
            return

        request = QgsFeatureRequest().setFilterFids(list(oid_of)).setNoAttributes()
        for feature in self._layer.getFeatures(request):
            oid = oid_of.get(feature.id())
            if oid is None or not feature.hasGeometry():
                continue
            old = self._highlights.pop(oid, None)
            if old is not None:
                self._drop_highlight(old)
            color = COLOR_LOADED if self._loaded.get(oid) else COLOR_EMPTY
            self._highlights[oid] = self._make_highlight(feature, color, 2)

    def _clear_active(self):
        if self._active is not None:
            self._drop_highlight(self._active)
            self._active = None

    def _clear_highlights(self):
        for highlight in self._highlights.values():
            self._drop_highlight(highlight)
        self._highlights = {}
        self._clear_active()

    def _mark_toggled(self, checked):
        if checked:
            self._mark(list(self._loaded))
            self._item_changed(self.list.currentItem(), None)
        else:
            self._clear_highlights()

    def _visibility_changed(self, visible):
        """No marks on the map while the panel cannot be seen."""
        if visible:
            self._mark(list(self._loaded))
        else:
            self._clear_highlights()

    # -- from picture to feature ------------------------------------------

    def _item_changed(self, current, _previous):
        """A picture was clicked: point out its feature on the map."""
        self._clear_active()
        fid = self._fid_of(current)
        if fid is None:
            return

        if self.mark_check.isChecked() and self.isVisible():
            feature = self._feature(fid)
            if feature is not None and feature.hasGeometry():
                self._active = self._make_highlight(feature, COLOR_ACTIVE, 4)

        self.iface.mapCanvas().flashFeatureIds(self._layer, [fid])

    def _context_menu(self, pos):
        entry = self.list.itemAt(pos)
        if entry is None:
            return
        self.list.setCurrentItem(entry)

        # addAction(text, slot) changed between Qt5 and Qt6, so the actions
        # are connected by hand.
        menu = QMenu(self)
        for text, slot in (
                ("Open picture", lambda: self._open_item(entry)),
                (None, None),
                ("Zoom to feature", self._zoom_to_current),
                ("Select feature", self._select_current),
                ("Show attribute form", self._form_current),
                (None, None),
                ("Save as…", self._save_item),
                ("Rotate thumbnail", self._rotate_item)):
            if text is None:
                menu.addSeparator()
            else:
                menu.addAction(text).triggered.connect(
                    lambda _checked=False, slot=slot: slot())
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _zoom_to_current(self):
        fid = self._fid_of(self.list.currentItem())
        if fid is None:
            return
        canvas = self.iface.mapCanvas()
        if self._layer.geometryType() == Qgis.GeometryType.Point:
            # Zooming to a point would zoom in as far as it goes; pan instead
            # and leave the scale alone.
            canvas.panToFeatureIds(self._layer, [fid])
        else:
            canvas.zoomToFeatureIds(self._layer, [fid])
        canvas.flashFeatureIds(self._layer, [fid])

    def _select_current(self):
        """Select the feature without the panel reacting to the selection.

        Otherwise, with Keep unticked, selecting one feature would replace
        every other picture in the panel.
        """
        fid = self._fid_of(self.list.currentItem())
        if fid is None:
            return
        self._suppress_selection = True
        try:
            self._layer.selectByIds([fid])
        finally:
            self._suppress_selection = False

    def _form_current(self):
        feature = self._feature(self._fid_of(self.list.currentItem()))
        if feature is not None:
            self._show_form(feature)

    # -- keep -------------------------------------------------------------

    def _pin_toggled(self, checked):
        if checked:
            return
        # Unticked: follow the layer tree and the selection again.
        active = self.iface.activeLayer()
        if active is not self._layer:
            self._bind_layer(active)
        else:
            self._reload()

    # -- attribute form ---------------------------------------------------

    def _show_form(self, feature):
        """Show the attribute form — one at a time.

        iface.openFeatureForm() hands back no reference to the dialog, so it
        cannot be closed again and every click would stack a new window on top
        of the last. QgsAttributeDialog can be held on to.
        """
        self._close_form()

        try:
            context = QgsAttributeEditorContext()
            context.setMapCanvas(self.iface.mapCanvas())

            # The feature must be kept alive on the Python side — the dialog
            # does not take ownership (featureOwner=False), it just points at it.
            self._form_feature = feature

            dialog = QgsAttributeDialog(
                self._layer, self._form_feature, False,
                self.iface.mainWindow(), True, context)
            dialog.setMode(QgsAttributeEditorContext.Mode.IdentifyMode)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

            if self._form_geometry is not None:
                dialog.setGeometry(self._form_geometry)

            dialog.show()
            dialog.raise_()
            self._form = dialog

        except Exception as exc:  # noqa: BLE001
            # The constructor signature has shifted between QGIS versions.
            QgsMessageLog.logMessage(
                "Falling back to openFeatureForm: {}".format(exc),
                LOG_TAG, Qgis.MessageLevel.Info)
            self._form = None
            self.iface.openFeatureForm(
                self._layer, feature, updateFeatureOnly=False, showModal=False)

    def _close_form(self):
        if self._form is not None:
            try:
                self._form_geometry = self._form.geometry()
                self._form.close()
                self._form.deleteLater()
            except RuntimeError:
                pass  # already gone
            self._form = None
        self._form_feature = None

    # -- actions ----------------------------------------------------------

    def _open_item(self, entry):
        # The file sits locally in the cache, so the system's default viewer
        # can open it without knowing anything about our token.
        QDesktopServices.openUrl(QUrl.fromLocalFile(entry.data(ROLE_PATH)))

    def _save_item(self):
        entry = self.list.currentItem()
        if entry is None:
            return
        source = entry.data(ROLE_PATH)
        target, _ = QFileDialog.getSaveFileName(
            self, "Save attachment", os.path.basename(source))
        if target:
            with open(source, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())

    def _rotate_item(self):
        """Fallback for images without a usable EXIF tag.

        Rotates the view in the list only; the file on disk is untouched.
        """
        entry = self.list.currentItem()
        if entry is None:
            return
        pixmap = entry.icon().pixmap(THUMB_SIZE, THUMB_SIZE)
        if pixmap.isNull():
            return
        entry.setIcon(QIcon(pixmap.transformed(
            QTransform().rotate(90), Qt.TransformationMode.SmoothTransformation)))

    def _update_cache_button(self):
        self.clear_button.setText(
            "Clear cache ({:.0f} MB)".format(
                cache_size(self._cache_dir) / 1048576.0))
        self.clear_button.setToolTip(self._cache_dir)

    def _clear_cache(self):
        # The files behind the thumbnails are about to go
        self._reset()
        removed, freed = prune_cache(self._cache_dir, max_age_days=0, max_bytes=0)
        self._update_cache_button()
        self.status.setText("Cleared {} file(s), {:.1f} MB.".format(
            removed, freed / 1048576.0))

    # -- teardown ---------------------------------------------------------

    def cleanup(self):
        """Called from the plugin's unload()."""
        self._debounce.stop()
        self._close_form()
        self._clear_highlights()

        for attr in ("_task", "_discover"):
            task = getattr(self, attr, None)
            if task is None:
                continue
            try:
                task.ready.disconnect()
                task.failed.disconnect()
                task.cancel()
            except (TypeError, RuntimeError):
                pass
            setattr(self, attr, None)

        if self._layer is not None:
            try:
                self._layer.selectionChanged.disconnect(self._selection_changed)
                self._layer.willBeDeleted.disconnect(self._layer_deleted)
            except (TypeError, RuntimeError):
                pass
            self._layer = None

        for signal, slot in (
                (self.visibilityChanged, self._visibility_changed),
                (QgsProject.instance().cleared, self._forget_sources),
                (self.iface.layerTreeView().currentLayerChanged,
                 self._layer_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def closeEvent(self, event):
        self._stop()
        super().closeEvent(event)
