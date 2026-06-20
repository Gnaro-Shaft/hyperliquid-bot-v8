"""Assure que la racine du repo est dans sys.path pour les imports (config, utils, ...)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
