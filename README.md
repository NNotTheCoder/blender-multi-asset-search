# Multi-Platform Asset Search Aggregator for Blender

A free, open-source Blender addon to search and auto-import 3D assets directly from **Sketchfab**, **Poly Haven**, and **Poly Pizza** into your 3D Viewport without leaving Blender.

---

## Features

- **Multi-Platform Search:** Aggregate results simultaneously across Sketchfab, Poly Haven, and Poly Pizza.
- **One-Click Auto-Import:** Downloads, extracts `.zip` bundles (glTF/bin/textures), and imports `.gltf`, `.glb`, or `.obj` files directly into your active Blender scene.
- **Non-Blocking Execution:** Asynchronous background fetching keeps the Blender UI smooth and responsive.
- **Client-Side Filtering:** Instantly filter by platform or import type (Direct Import vs. Web Page).
- **Secure Token Storage:** Key fields use native Blender password fields saved locally in Addon Preferences.

---

## Supported Blender Versions

Tested and supported on **Blender 3.4.0 through 4.x+**.

> **Note:** Ensure Blender's built-in **Import-Export: glTF 2.0** and **Wavefront OBJ** addons are enabled.

---

## Installation

1. Download the latest release `.py` file (or clone this repository).
2. Open Blender and go to **Edit > Preferences > Add-ons**.
3. Click **Install...** at the top right, select `multi_platform_asset_search.py`, and click **Install Add-on**.
4. Check the box next to **Multi-Platform Asset Search Aggregator** to enable it.

---

## Setup & API Keys

Poly Haven requires **no API key**. To search Sketchfab and Poly Pizza, enter your personal keys in the addon preferences:

1. Go to **Edit > Preferences > Add-ons > Multi-Platform Asset Search Aggregator**.
2. Expand the preferences dropdown:
   - **Sketchfab Token:** Get one at [sketchfab.com/settings/password](https://sketchfab.com/settings/password) (API tab).
   - **Poly Pizza Key:** Get one at [poly.pizza/settings/api](https://poly.pizza/settings/api).
3. Keys save automatically to your Blender user profile.

---

## Usage

1. Open the 3D Viewport sidebar (press `N` if hidden).
2. Click the **Asset Search** tab.
3. Enter a search term (e.g., `chair`, `bottle`) and click **Go**.
4. Click on any asset result to import it into your scene.

---

## License

Distributed under the **GNU General Public License v3.0 (GPL-3.0)**. See `LICENSE` for details.
