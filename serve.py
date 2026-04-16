"""
Launcher voor de BPMN Inventory webapp.

Gebruik:  python serve.py           (poort 8095)
Of:       run_web.bat               (dubbelklik in Verkenner)
"""
from src.webapp import app

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8095, debug=False)
