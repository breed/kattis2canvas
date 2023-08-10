#!/bin/bash

rm -rf build
mkdir build
PYTHONUSERBASE=$PWD/build python3 -m pip install --ignore-installed bs4 click canvasapi typing-extensions
mkdir build/kattis2canvas.app
mv build/lib/*/site-packages/* build/kattis2canvas.app
cp kattis2canvas.py build/kattis2canvas.app/__main__.py
python3 -m zipapp build/kattis2canvas.app
