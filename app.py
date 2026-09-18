# app.py — Entry point for AI Dubbing Pro (PyQt5)
# Launches dubber_pyqt5.py as the main application

import subprocess
import sys
import os

if __name__ == "__main__":
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dubber_pyqt5.py")
    subprocess.run([sys.executable, script])
