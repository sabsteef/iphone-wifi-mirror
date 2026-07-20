"""py2app build script for iPhone WiFi Mirror.

Usage:
    ./.venv/bin/pip install py2app
    ./.venv/bin/python setup.py py2app

Produces dist/iPhone Mirror.app which can be dragged into /Applications.
"""
from setuptools import setup

APP = ["main.py"]
APP_NAME = "iPhone Mirror"

# Every non-Python resource file we want to ship inside the bundle.
DATA_FILES = []

# py2app options.
OPTIONS = {
    "argv_emulation": False,  # do NOT swap argv from Finder — we don't need it
    "iconfile": "resources/AppIcon.icns",
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.iphonewifimirror.app",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        # macOS may need this for local network permission on Sonoma+
        "NSLocalNetworkUsageDescription":
            "iPhone Mirror needs local network access to discover and "
            "connect to your iPhone over WiFi.",
        "NSBonjourServices": [
            "_apple-mobdev2._tcp",
            "_remoted._tcp",
            "_remotepairing._tcp",
        ],
    },
    # Force-include modules py2app can't discover statically. pymobiledevice3
    # loads a lot of submodules dynamically; enumerate the ones that get
    # imported from strings.
    "packages": [
        "pymobiledevice3",
        "PyQt6",
        "qasync",
        "requests",
        "keyring",
        "PIL",
        "cryptography",
        "src",
    ],
    "includes": [
        "asyncio",
        "concurrent.futures",
        "signal",
        "threading",
    ],
    # Trim py2app's stdlib copy — we don't need Tk/pyparsing/etc.
    "excludes": [
        "tkinter",
        "pytest",
        "test",
        "tests",
    ],
    "arch": "arm64",  # Apple Silicon; change to "universal2" for both.
    # semi-standalone: link against the system Python framework instead of
    # copying it in. Cuts bundle size + avoids Python-version headaches.
    "semi_standalone": False,
}

setup(
    app=APP,
    name=APP_NAME,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
