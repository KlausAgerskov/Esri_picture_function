# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Naturstyrelsen
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Esri Picture Function
---------------------
Shows pictures attached to ArcGIS Feature Service layers in QGIS, and makes
them available to print layouts and atlas.
"""


def classFactory(iface):  # noqa: N802 — the name is fixed by QGIS
    from .esri_picture_function import EsriPictureFunction
    return EsriPictureFunction(iface)
