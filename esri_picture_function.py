# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Naturstyrelsen
# SPDX-License-Identifier: GPL-2.0-or-later
"""
esri_picture_function.py
------------------------
The main class. Creates the toolbar and menu entries, owns the attachment
panel, registers the layout expression functions, and drives the export that
makes the images available to an atlas. The export is started from the panel.
"""

import os

from qgis.PyQt.QtCore import Qt, QObject, QCoreApplication
from qgis.PyQt.QtGui import QIcon, QGuiApplication
from qgis.PyQt.QtWidgets import QFileDialog

try:
    from qgis.PyQt.QtGui import QAction       # Qt6: QAction lives in QtGui
except ImportError:
    from qgis.PyQt.QtWidgets import QAction   # Qt5

from qgis.core import Qgis

from qgis.PyQt import sip

from .attachment_dock import AttachmentDock
from .layout_export import (
    export_layer_photos, photo_dir, register_functions, unregister_functions,
)

MENU_NAME = u"&Esri Picture Function"
TOOLBAR_NAME = u"Esri Picture Function"

TEMPLATE_NAME = "picture_table.html"


def delete_now(obj):
    """Delete a Qt object right away.

    deleteLater() is deferred to the next pass of the event loop. Plugin
    Reloader checks for leftover widgets immediately after unload(), and so
    still finds them, with the warning "removing duplicated widget(s) not
    cleaned up by the plugin during unload". sip.delete() tears the object
    down now and makes the cleanup visible to the reloader.
    """
    if obj is None:
        return
    try:
        if not sip.isdeleted(obj):
            sip.delete(obj)
    except (RuntimeError, TypeError, ValueError, AttributeError):
        try:
            obj.deleteLater()
        except RuntimeError:
            pass


class EsriPictureFunction(QObject):
    """QGIS plugin: show ArcGIS attachments and put them into a layout."""

    def __init__(self, iface):
        # deliberately without a parent: were the plugin a child of the main
        # window, the old instance would survive every reload
        super(EsriPictureFunction, self).__init__()
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.dock = None
        self.toolbar = None
        self.actionDock = None
        self.actionTemplate = None

    def tr(self, message):
        return QCoreApplication.translate("EsriPictureFunction", message)

    # -- building the GUI --------------------------------------------------

    def _icon(self, filename):
        return QIcon(os.path.join(self.plugin_dir, "images", filename))

    def _add_action(self, icon, text, callback, checkable=False, tooltip=None):
        action = QAction(icon, text, self.iface.mainWindow())
        action.setCheckable(checkable)
        if checkable:
            action.toggled.connect(callback)
        else:
            action.triggered.connect(callback)
        if tooltip:
            action.setToolTip(tooltip)
            action.setStatusTip(tooltip)
        self.toolbar.addAction(action)
        self.iface.addPluginToMenu(MENU_NAME, action)
        self.actions.append(action)
        return action

    def initGui(self):
        self.toolbar = self.iface.addToolBar(TOOLBAR_NAME)
        self.toolbar.setObjectName("EsriPictureFunctionToolbar")

        self.dock = AttachmentDock(self.iface, self.iface.mainWindow())
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.hide()

        self.actionDock = self._add_action(
            self._icon("panel.svg"),
            self.tr("Attachment pictures"),
            self._toggle_dock,
            checkable=True,
            tooltip=self.tr("Show the attachment picture panel"))
        self.dock.visibilityChanged.connect(self._dock_visibility_changed)
        self.dock.export_requested.connect(self._export_photos)

        self.actionTemplate = self._add_action(
            self._icon("template.svg"),
            self.tr("Copy HTML template"),
            self._copy_template,
            tooltip=self.tr("Put the layout HTML item template on the "
                            "clipboard"))

        register_functions()

    def unload(self):
        """Remove everything the plugin put into the user interface.

        The order matters: signals are disconnected first, so nothing is
        called on half-demolished objects, and each widget is removed from the
        main window before it is deleted, so the main window is not left
        holding a dead child reference.
        """
        unregister_functions()

        if self.dock is not None:
            try:
                self.dock.visibilityChanged.disconnect(
                    self._dock_visibility_changed)
                self.dock.export_requested.disconnect(self._export_photos)
            except (TypeError, RuntimeError):
                pass
            try:
                self.dock.cleanup()
            except RuntimeError:
                pass
            self.iface.removeDockWidget(self.dock)
            self.dock.setParent(None)
            delete_now(self.dock)
            self.dock = None

        for action in self.actions:
            self.iface.removePluginMenu(MENU_NAME, action)
            self.iface.removeToolBarIcon(action)
            if self.toolbar is not None:
                self.toolbar.removeAction(action)
            action.setParent(None)
            delete_now(action)
        self.actions = []
        self.actionDock = None
        self.actionTemplate = None

        # removeToolBar() takes it out of the main window layout but leaves
        # the main window as its parent, so it must be deleted too
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.setParent(None)
            delete_now(self.toolbar)
            self.toolbar = None

    # -- actions -----------------------------------------------------------

    def _toggle_dock(self, checked):
        if self.dock is None:
            return
        self.dock.setVisible(checked)

    def _dock_visibility_changed(self, visible):
        """Keep the toolbar button in sync with the visibility of the panel."""
        if self.actionDock is None or sip.isdeleted(self.actionDock):
            return
        if self.actionDock.isChecked() != visible:
            self.actionDock.blockSignals(True)
            self.actionDock.setChecked(visible)
            self.actionDock.blockSignals(False)

    def _export_photos(self):
        """Save the pictures of the features in the panel to a folder.

        The panel has already worked out the service URL, the authcfg and
        where the attachments hang, so the export reuses that rather than
        asking the server again. It takes every feature the panel holds —
        with Keep ticked that is more than the current selection.
        """
        if self.dock is None or self.dock.layer is None:
            self._warn(self.tr("Select an ArcGIS layer first."))
            return

        source = self.dock.current_source
        if source is None:
            self._warn(self.tr("The panel found no attachments on this "
                               "layer."))
            return

        oids = self.dock.panel_oids
        if not oids:
            self._warn(self.tr("Select the features to include in the "
                               "layout."))
            return

        out_dir = QFileDialog.getExistingDirectory(
            self.iface.mainWindow(),
            self.tr("Folder for layout pictures"),
            photo_dir() or "")
        if not out_dir:
            return

        self.iface.messageBar().pushInfo(
            TOOLBAR_NAME,
            self.tr("Fetching pictures for {} feature(s)…").format(len(oids)))

        export_layer_photos(
            self.dock.layer,
            self.dock.service_url,
            self.dock.authcfg,
            source,
            out_dir,
            oids=oids,
            on_done=self._export_done,
            on_error=self._export_failed,
        )

    def _export_done(self, features, images):
        self.iface.messageBar().pushMessage(
            TOOLBAR_NAME,
            self.tr("{} picture(s) from {} feature(s). Remember to save the "
                    "project — the folder is stored in the project "
                    "file.").format(images, features),
            level=Qgis.MessageLevel.Success, duration=8)

    def _export_failed(self, message):
        self.iface.messageBar().pushMessage(
            TOOLBAR_NAME, message, level=Qgis.MessageLevel.Critical, duration=10)

    def _copy_template(self):
        """Put the HTML item template on the clipboard.

        Easier than telling people to find a file inside the plugin folder:
        the layout's HTML item takes a paste directly.
        """
        path = os.path.join(self.plugin_dir, "templates", TEMPLATE_NAME)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            self._warn(self.tr("Could not read the template: {}").format(exc))
            return

        QGuiApplication.clipboard().setText(text)
        self.iface.messageBar().pushMessage(
            TOOLBAR_NAME,
            self.tr("Template copied. Paste it into the layout's HTML item "
                    "and tick \"Evaluate QGIS expressions in HTML source\"."),
            level=Qgis.MessageLevel.Info, duration=10)

    # -- helpers -----------------------------------------------------------

    def _warn(self, message):
        self.iface.messageBar().pushMessage(
            TOOLBAR_NAME, message, level=Qgis.MessageLevel.Warning, duration=6)
