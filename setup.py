"""py2app build script for iPhone WiFi Mirror.

Build:
    ./.venv/bin/pip install py2app
    ./.venv/bin/python setup.py py2app

Install:
    cp -R "dist/iPhone Mirror.app" /Applications/
"""
from setuptools import setup

APP = ["main.py"]
APP_NAME = "iPhone Mirror"

DATA_FILES = []

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "resources/AppIcon.icns",
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.iphonewifimirror.app",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "14.0",
        "NSLocalNetworkUsageDescription":
            "iPhone Mirror needs local network access to discover and "
            "connect to your iPhone over WiFi.",
        "NSBonjourServices": [
            "_apple-mobdev2._tcp",
            "_remoted._tcp",
            "_remotepairing._tcp",
        ],
    },
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
    "excludes": [
        "tkinter",
        "pytest",
        "test",
        "tests",
    ],
    "arch": "arm64",
    "semi_standalone": False,
}

setup(
    app=APP,
    name=APP_NAME,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
