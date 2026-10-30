from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


STYLE = """
* { font-family: 'Segoe UI', 'Microsoft YaHei UI', sans-serif; }
QMainWindow, QWidget#mainRoot { background: #f4f6f9; color: #172235; }
QWidget#sidebar { background: #101b2d; }
QLabel#brandMark { color: #ffffff; font-size: 19px; font-weight: 700; }
QLabel#brandSub { color: #92a4bd; font-size: 11px; }
QPushButton#navButton { color: #aebbd0; background: transparent; border: 0; border-radius: 9px; text-align: left; padding: 12px 14px; font-size: 14px; }
QPushButton#navButton:hover { color: white; background: #1b2940; }
QPushButton#navButton:checked { color: white; background: #244a43; font-weight: 600; }
QLabel#pageTitle { color: #132238; font-size: 25px; font-weight: 700; }
QLabel#pageSubtitle { color: #758196; font-size: 12px; }
QFrame#card { background: white; border: 1px solid #e3e8ef; border-radius: 13px; }
QFrame#heroCard { background: #12253a; border: 0; border-radius: 15px; }
QLabel#cardTitle { color: #24334a; font-size: 14px; font-weight: 650; }
QLabel#muted { color: #7a8799; font-size: 12px; }
QLabel#statValue { color: #17283f; font-size: 24px; font-weight: 700; }
QLabel#heroValue { color: white; font-size: 25px; font-weight: 700; }
QLabel#heroMuted { color: #b2c3d4; font-size: 12px; }
QPushButton { color: #223248; background: white; border: 1px solid #d8e0e9; border-radius: 8px; padding: 9px 14px; font-size: 13px; }
QPushButton:hover { background: #f3f7fa; border-color: #b9c8d8; }
QPushButton:pressed { background: #e8eef3; }
QPushButton:disabled { color: #aeb8c5; background: #f5f6f8; }
QPushButton#primaryButton { color: white; background: #117a5b; border: 1px solid #117a5b; font-weight: 600; }
QPushButton#primaryButton:hover { background: #0d6a4e; }
QPushButton#primaryButton:disabled { color: #aeb8c5; background: #f5f6f8; border-color: #d8e0e9; }
QPushButton#dangerButton { color: #a23a3a; border-color: #eccaca; }
QLineEdit, QComboBox { background: white; border: 1px solid #d9e1ea; border-radius: 7px; padding: 8px 10px; min-height: 22px; color: #24334a; }
QComboBox::drop-down { border: 0; width: 24px; }
QCheckBox { color: #34445b; spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QTableWidget { background: white; alternate-background-color: #f8fafc; border: 1px solid #e3e8ef; border-radius: 9px; gridline-color: #edf0f4; selection-background-color: #e3f3ed; selection-color: #18372f; }
QHeaderView::section { color: #65748a; background: #f8fafc; border: 0; border-bottom: 1px solid #e3e8ef; padding: 9px; font-weight: 600; }
QProgressBar { border: 0; background: #e8edf2; border-radius: 4px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #13916b; border-radius: 4px; }
QStatusBar { background: white; border-top: 1px solid #e3e8ef; color: #68768a; }
QScrollArea { border: 0; background: transparent; }
"""


def card(title: str, subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 17)
    layout.setSpacing(10)
    head = QVBoxLayout()
    head.setSpacing(3)
    title_label = QLabel(title)
    title_label.setObjectName("cardTitle")
    head.addWidget(title_label)
    if subtitle:
        note = QLabel(subtitle)
        note.setObjectName("muted")
        note.setWordWrap(True)
        head.addWidget(note)
    layout.addLayout(head)
    return frame, layout


def stat_card(title: str, value: str, note: str) -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(17, 16, 17, 16)
    layout.setSpacing(6)
    name = QLabel(title)
    name.setObjectName("muted")
    number = QLabel(value)
    number.setObjectName("statValue")
    detail = QLabel(note)
    detail.setObjectName("muted")
    detail.setWordWrap(True)
    layout.addWidget(name)
    layout.addWidget(number)
    layout.addWidget(detail)
    frame.value_label = number  # type: ignore[attr-defined]
    frame.note_label = detail  # type: ignore[attr-defined]
    return frame
