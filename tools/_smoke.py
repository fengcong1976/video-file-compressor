import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
import main as m
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

app = QApplication(sys.argv)
m.configure_app(app)
w = m.MainWindow()
w.show()
app.processEvents()
print("ffmpeg_ok", w.ffmpeg_ok)
print("win", w.width(), w.height(), "min", w.minimumSizeHint().width())
print("cols", [w.table.horizontalHeaderItem(c).text() for c in range(w.table.columnCount())])
print("preset items", [w.preset.itemText(i) for i in range(w.preset.count())])
print("preset width", w.preset.width(), "hint", w.preset.sizeHint().width())
w.grab().save("_smoke.png")
app.quit()
