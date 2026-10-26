# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Naturstyrelsen
# SPDX-License-Identifier: GPL-2.0-or-later
"""
layout_export.py
----------------
Makes ArcGIS attachments usable in a QGIS print layout.

Neither the picture item nor the HTML item can fetch from a secured URL, so
the images are exported to a folder with predictable names first. After that
a picture item's source can be data-defined with esri_picture(), or an HTML
item can build a list with esri_pictures(), while atlas iterates the layer.

Two things are fixed at export time rather than at render time:

* EXIF orientation is applied to the file itself. The layout's HTML item
  ignores EXIF orientation — QtWebKit in QGIS 3 does not honour
  image-orientation: from-image — and so does the picture item. Rotating once on export
  is both correct and cheaper than rotating on every redraw.
* Images are capped to a maximum edge length. A layout does not benefit from
  12 megapixels, and the exported PDF shrinks dramatically.
"""

import glob
import os

from qgis.PyQt.QtCore import (
    QBuffer, QByteArray, QIODevice, QMetaType, Qt, pyqtSignal,
)
from qgis.PyQt.QtGui import QImageReader, QImageWriter

from qgis.core import (
    Qgis, QgsApplication, QgsExpression, QgsField, QgsMessageLog, QgsProject,
    QgsTask, qgsfunction,
)

from .attachment_dock import RestClient, feature_oid, oid_field_name

LOG_TAG = "Esri Pictures"

#: Project key, so the folder survives closing and reopening the project
PROJECT_SCOPE = "esri_picture_function"
PROJECT_KEY = "dir"

#: Longest edge of an exported image, in pixels. 1600 is ample for a
#: half-page picture at 300 dpi.
DEFAULT_MAX_PIXELS = 1600

#: JPEG quality for re-encoded images
JPEG_QUALITY = 88

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif",
                    ".tiff")


# --------------------------------------------------------------------------
# Expression functions
# --------------------------------------------------------------------------

def photo_dir():
    """The export folder as stored in the project."""
    value, ok = QgsProject.instance().readEntry(PROJECT_SCOPE, PROJECT_KEY)
    return value if ok and value else ""


def set_photo_dir(path):
    QgsProject.instance().writeEntry(PROJECT_SCOPE, PROJECT_KEY, path)


def _photo_index(path):
    """The running number in <oid>_<n>.<ext>, for natural sorting.

    Without it _10 sorts before _2, because glob returns alphabetical order.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        return int(stem.rsplit("_", 1)[-1])
    except ValueError:
        return 0


def _photo_paths(oid):
    """Every exported picture for a feature, sorted, with forward slashes.

    Windows backslashes do not work in a file:/// URL in the HTML item, and Qt
    accepts forward slashes everywhere, so they are normalised once here.
    """
    directory = photo_dir()
    if not directory or oid is None:
        return []

    matches = glob.glob(os.path.join(directory, "{}_*".format(int(oid))))
    matches.sort(key=_photo_index)
    return [path.replace("\\", "/") for path in matches]


@qgsfunction(group="Esri Pictures", referenced_columns=[], register=False)
def esri_picture(oid, index, feature, parent):
    """Path to the index'th exported picture for a feature, else ''.

    Use it in a picture item's data-defined Image source:

        esri_picture("OBJECTID", 1)

    An empty string means "no picture" — the layout item then draws nothing
    rather than a broken icon.
    """
    paths = _photo_paths(oid)
    position = int(index) - 1
    return paths[position] if 0 <= position < len(paths) else ""


@qgsfunction(group="Esri Pictures", referenced_columns=[], register=False)
def esri_pictures(oid, feature, parent):
    """Every exported picture for a feature, as an array of paths.

    For the HTML item in a layout, where the number of pictures varies:

        array_foreach(
            esri_pictures(attribute(@atlas_feature, 'OBJECTID')),
            '<img src="file:///' || @element || '">'
        )

    The paths use forward slashes and go straight into a file:/// URL.
    """
    return _photo_paths(oid)


def register_functions():
    """Called from the plugin's initGui().

    The decorators carry register=False, so registration happens here and only
    here — otherwise importing the module would register the functions, and
    reloading the plugin would raise "function already registered".
    """
    QgsExpression.registerFunction(esri_picture)
    QgsExpression.registerFunction(esri_pictures)


def unregister_functions():
    """Called from the plugin's unload(). Without it the next reload fails."""
    for name in ("esri_picture", "esri_pictures"):
        # Returns False rather than raising when the function is not
        # registered, so no exception handling is needed.
        QgsExpression.unregisterFunction(name)


# --------------------------------------------------------------------------
# Image preparation
# --------------------------------------------------------------------------

def prepare_image(data, path, max_pixels=DEFAULT_MAX_PIXELS):
    """Write image bytes to disk, EXIF-rotated and size-capped.

    Returns True when the image was written, False when the payload could not
    be decoded — the caller then stores the original bytes untouched, which is
    what should happen for PDFs and other non-images.
    """
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)

    reader = QImageReader(buffer)
    # This is the whole point: apply the EXIF rotation tag now, once, because
    # the layout renderer will not do it later.
    reader.setAutoTransform(True)

    image = reader.read()
    buffer.close()
    if image.isNull():
        return False

    longest = max(image.width(), image.height())
    if max_pixels and longest > max_pixels:
        if image.width() >= image.height():
            image = image.scaledToWidth(
                max_pixels, Qt.TransformationMode.SmoothTransformation)
        else:
            image = image.scaledToHeight(
                max_pixels, Qt.TransformationMode.SmoothTransformation)

    writer = QImageWriter(path)
    if os.path.splitext(path)[1].lower() in (".jpg", ".jpeg"):
        writer.setQuality(JPEG_QUALITY)
    return writer.write(image)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

class ExportTask(QgsTask):
    """Fetch the attachments of a whole layer and write them to disk.

    File names are <parent object id>_<running number><extension>, so
    esri_picture() can find them again without an index or a manifest file.
    """

    ready = pyqtSignal(int, int)      # (features, images)
    failed = pyqtSignal(str)

    def __init__(self, layer_url, authcfg, source, oids, out_dir,
                 max_per_feature=3, max_pixels=DEFAULT_MAX_PIXELS):
        super().__init__("Exporting pictures for layout", QgsTask.Flag.CanCancel)
        self._layer_url = layer_url.rstrip("/")
        self._client = RestClient(authcfg)
        self._source = source
        self._oids = list(oids)
        self._out_dir = out_dir
        self._max = max_per_feature
        self._max_pixels = max_pixels
        self._features = 0
        self._images = 0
        self._error = ""

    # -- lookups ----------------------------------------------------------

    def _related(self, parent_oids):
        """parent object id -> [child object id] via queryRelatedRecords."""
        payload = self._client.get_json(
            "{}/queryRelatedRecords".format(self._layer_url),
            objectIds=",".join(str(o) for o in parent_oids),
            relationshipId=self._source.relationship_id,
            outFields=self._source.target_oid_field,
            returnGeometry="false",
            f="json",
        )
        oid_field = payload.get("objectIdFieldName") or self._source.target_oid_field

        mapping = {}
        for group in payload.get("relatedRecordGroups", []):
            parent = group.get("objectId")
            children = []
            for record in group.get("relatedRecords", []):
                value = record.get("attributes", {}).get(oid_field)
                if value is not None:
                    children.append(int(value))
            if parent is not None:
                mapping[int(parent)] = children
        return mapping

    def _attachments(self, oids):
        """child object id -> [attachmentInfo]."""
        payload = self._client.get_json(
            "{}/queryAttachments".format(self._source.target_url),
            objectIds=",".join(str(o) for o in oids),
            returnUrl="false",
            f="json",
        )
        mapping = {}
        for group in payload.get("attachmentGroups", []):
            oid = group.get("parentObjectId")
            if oid is not None:
                # Sort on attachment id: the server guarantees no order, and
                # without this the first picture could be a different one on
                # the next run — which would change an approved layout.
                mapping[int(oid)] = sorted(
                    group.get("attachmentInfos", []),
                    key=lambda info: info.get("id", 0))
        return mapping

    def _write(self, data, path):
        """Write one attachment, re-encoding it when it is an image."""
        if os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS:
            try:
                if prepare_image(data, path, self._max_pixels):
                    return
            except Exception as exc:  # noqa: BLE001 — fall back to raw bytes
                QgsMessageLog.logMessage(
                    "Could not re-encode {}: {}".format(os.path.basename(path), exc),
                    LOG_TAG, Qgis.MessageLevel.Info)

        with open(path, "wb") as handle:
            handle.write(data)

    # -- QgsTask ----------------------------------------------------------

    def run(self):
        try:
            os.makedirs(self._out_dir, exist_ok=True)

            # The server limits URL length, so work in batches.
            total = max(len(self._oids), 1)
            done = 0

            for start in range(0, len(self._oids), 50):
                if self.isCanceled():
                    return False

                batch = self._oids[start:start + 50]

                if self._source.relationship_id is None:
                    mapping = {oid: [oid] for oid in batch}
                else:
                    mapping = self._related(batch)

                child_oids = [c for children in mapping.values() for c in children]
                if not child_oids:
                    done += len(batch)
                    continue

                infos = self._attachments(child_oids)

                for parent_oid, children in mapping.items():
                    if self.isCanceled():
                        return False

                    # Drop the previous run's files for this feature, or a
                    # deleted picture lingers as number 3 in the layout.
                    for stale in glob.glob(os.path.join(
                            self._out_dir, "{}_*".format(parent_oid))):
                        try:
                            os.remove(stale)
                        except OSError:
                            pass

                    index = 0
                    for child in sorted(children):
                        for info in infos.get(child, []):
                            if index >= self._max:
                                break
                            index += 1

                            ext = os.path.splitext(info.get("name", ""))[1] or ".jpg"
                            path = os.path.join(
                                self._out_dir,
                                "{}_{}{}".format(parent_oid, index, ext))

                            url = "{}/{}/attachments/{}".format(
                                self._source.target_url, child, info["id"])
                            self._write(self._client.get(url), path)

                            self._images += 1

                    if index:
                        self._features += 1

                done += len(batch)
                self.setProgress(100.0 * done / total)

            return True

        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            return False

    def finished(self, result):
        if result:
            set_photo_dir(self._out_dir)
            self.ready.emit(self._features, self._images)
        elif not self.isCanceled():
            QgsMessageLog.logMessage(self._error, LOG_TAG, Qgis.MessageLevel.Warning)
            self.failed.emit(self._error)


# --------------------------------------------------------------------------
# Setup helpers
# --------------------------------------------------------------------------

def add_photo_fields(layer, count=3):
    """Add virtual fields picture1..pictureN to the layer.

    Useful when the layout has to open without the plugin loaded — but note
    that a virtual field is evaluated on every redraw, so esri_picture()
    straight in the data-defined picture source is faster.
    """
    oid_field = oid_field_name(layer)
    if oid_field is None:
        raise RuntimeError("No object id field found on the layer")

    existing = [f.name() for f in layer.fields()]
    for index in range(1, count + 1):
        name = "picture{}".format(index)
        if name in existing:
            continue
        layer.addExpressionField(
            'esri_picture("{}", {})'.format(oid_field, index),
            QgsField(name, QMetaType.Type.QString),
        )


def export_layer_photos(layer, layer_url, authcfg, source, out_dir,
                        selected_only=False, max_per_feature=3,
                        max_pixels=DEFAULT_MAX_PIXELS,
                        on_done=None, on_error=None, oids=None):
    """Start the export. Returns the task so the caller can cancel it.

    oids, when given, are the object ids to export — the panel passes the
    features it holds. Otherwise the layer's selection, or the whole layer.
    """
    if oids is None:
        oid_field = oid_field_name(layer)
        features = layer.selectedFeatures() if selected_only else layer.getFeatures()
        oids = [feature_oid(layer, f, oid_field) for f in features]

    task = ExportTask(layer_url, authcfg, source, oids, out_dir,
                      max_per_feature, max_pixels)
    if on_done is not None:
        task.ready.connect(on_done)
    if on_error is not None:
        task.failed.connect(on_error)
    QgsApplication.taskManager().addTask(task)
    return task
