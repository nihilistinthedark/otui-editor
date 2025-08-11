
import sys, os, re, shutil
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QSplitter, QWidget, QVBoxLayout,
    QHBoxLayout, QPushButton, QTreeWidget, QTreeWidgetItem, QLabel, QLineEdit,
    QFormLayout, QMessageBox, QToolBar, QStatusBar, QSizePolicy, QPlainTextEdit
)
from PySide6.QtGui import QPixmap, QAction, QPainter
from PySide6.QtCore import Qt, QTimer, QEvent

# ==============
# Parser OTUI/OTML simplificado (fiel ao esquema de indentação) + serializer
# ==============
@dataclass
class OTUINode:
    tag: str = ""
    value: str = ""
    depth: int = 0
    children: List["OTUINode"] = field(default_factory=list)

class OTUIParser:
    def __init__(self, indent_size: int = 2):
        self.indent_size = indent_size

    def parse_text(self, text: str) -> OTUINode:
        lines = text.splitlines()
        root = OTUINode("Root", "", -1, [])
        stack = [root]

        def depth_of(raw: str, stripped: str) -> int:
            return (len(raw) - len(stripped)) // self.indent_size

        for raw in lines:
            raw_line = raw.rstrip("\n")
            stripped = raw_line.lstrip()
            if stripped == "":
                continue
            # comentários //...
            if stripped.startswith("//"):
                d = depth_of(raw_line, stripped)
                while stack and d <= stack[-1].depth: stack.pop()
                node = OTUINode("//", stripped[2:].strip(), d, [])
                stack[-1].children.append(node)
                stack.append(node)
                continue
            d = depth_of(raw_line, stripped)
            while stack and d <= stack[-1].depth: stack.pop()
            if ":" in stripped:
                tag, val = stripped.split(":", 1)
                tag = tag.strip(); val = val.strip()
            else:
                tag = stripped.strip(); val = ""
            node = OTUINode(tag, val, d, [])
            stack[-1].children.append(node)
            stack.append(node)
        return root

    def to_string(self, root: OTUINode) -> str:
        out = []
        def write(n: OTUINode, d: int):
            if n.tag == "Root":
                for ch in n.children: write(ch, 0)
                return
            indent = " " * (d * self.indent_size)
            if n.tag == "//":
                out.append(f"{indent}// {n.value}")
            else:
                if n.value:
                    out.append(f"{indent}{n.tag}: {n.value}")
                else:
                    out.append(f"{indent}{n.tag}")
                for ch in n.children:
                    write(ch, d+1)
        write(root, 0)
        return "\n".join(out) + ("\n" if out else "")

# ==============
# Utilidades de imagens (com varredura opcional de .lua)
# ==============
IMG_EXTS = [".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"]
LIKELY_DIRS = ["images", "textures", "icons", "img", "resources", "assets"]

def collect_image_sources(root: OTUINode) -> List[str]:
    found = []
    def walk(n: OTUINode):
        if n.tag.lower() == "image-source" and n.value:
            v = n.value.strip().strip("'").strip('"')
            found.append(v)
        for ch in n.children: walk(ch)
    walk(root)
    return found

def discover_images_base(otui_path: Path, root: OTUINode) -> Optional[Path]:
    mod_dir = otui_path.parent
    # 1) diretórios prováveis
    for d in LIKELY_DIRS:
        p = mod_dir / d
        if p.is_dir(): return p
    # 2) varrendo por nomes
    imgs = collect_image_sources(root)
    names = set(Path(v).name for v in imgs)
    hits = {}
    for r, _, files in os.walk(mod_dir):
        common = names.intersection(set(files))
        if common:
            hits[r] = len(common)
    if hits:
        best = max(hits.items(), key=lambda kv: kv[1])[0]
        return Path(best)
    # 3) heurística: varre .lua para detectar pastas onde há referências de imagem
    lua_hits = {}
    for r, _, files in os.walk(mod_dir):
        for f in files:
            if f.lower().endswith(".lua"):
                try:
                    txt = Path(r, f).read_text(encoding="utf-8", errors="ignore")
                    for m in re.finditer(r'"([^"]+\.(?:png|jpg|jpeg|bmp|gif|webp))"', txt, re.IGNORECASE):
                        p = Path(m.group(1))
                        folder = (Path(r) / p).parent
                        lua_hits[str(folder)] = lua_hits.get(str(folder), 0) + 1
                except Exception:
                    pass
    if lua_hits:
        best = max(lua_hits.items(), key=lambda kv: kv[1])[0]
        bp = Path(best)
        if bp.is_dir(): return bp
    return None

def resolve_image(images_base: Optional[Path], otui_dir: Path, val: str) -> Optional[Path]:
    if not val: return None
    v = val.strip().strip("'").strip('"').replace("\\", "/").lstrip("/")
    candidates = []
    # base
    if images_base:
        base = images_base
        p = base / v
        root, ext = os.path.splitext(p.name)
        if ext:
            candidates.append(p)
        else:
            for e in IMG_EXTS: candidates.append(base / (v + e))
    # relativo ao .otui
    p2 = otui_dir / v
    root2, ext2 = os.path.splitext(p2.name)
    if ext2:
        candidates.append(p2)
    else:
        for e in IMG_EXTS: candidates.append(otui_dir / (v + e))
    # por basename
    base_name = Path(v).name.lower()
    for r, _, files in os.walk(otui_dir):
        for f in files:
            if f.lower() == base_name:
                candidates.append(Path(r) / f)
    seen = set()
    for c in candidates:
        c = c.resolve()
        if c in seen: continue
        seen.add(c)
        if c.exists(): return c
    return None

# ==============
# Editor PySide6 – com texto OTUI em tempo real
# ==============
class OTUIEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OTUI Editor – PRO (PySide6)")
        self.resize(1400, 800)

        self.parser = OTUIParser()
        self.current_file: Optional[Path] = None
        self.current_root: Optional[OTUINode] = None
        self.images_base: Optional[Path] = None

        self.undo_stack: List[str] = []
        self.redo_stack: List[str] = []

        self.offset_x = 0
        self.offset_y = 0
        self.last_coords = (-1, -1)
        self._last_preview_item = None

        self._build_ui()
        self._setup_live_parse()

    def _build_ui(self):
        tb = QToolBar("Ações", self); self.addToolBar(tb)
        act_open = QAction("Abrir", self); act_open.triggered.connect(self.open_file); tb.addAction(act_open)
        act_save = QAction("Salvar", self); act_save.triggered.connect(self.save_file); tb.addAction(act_save)
        act_saveas = QAction("Salvar como...", self); act_saveas.triggered.connect(self.save_as); tb.addAction(act_saveas)
        tb.addSeparator()
        act_undo = QAction("Undo", self); act_undo.triggered.connect(self.undo); tb.addAction(act_undo)
        act_redo = QAction("Redo", self); act_redo.triggered.connect(self.redo); tb.addAction(act_redo)
        tb.addSeparator()
        act_setimg = QAction("Definir pasta de imagens...", self); act_setimg.triggered.connect(self.choose_images_base); tb.addAction(act_setimg)
        act_autoimg = QAction("Descobrir pasta de imagens", self); act_autoimg.triggered.connect(self.auto_discover_images); tb.addAction(act_autoimg)

        self.status = QStatusBar(self); self.setStatusBar(self.status)

        # Layout principal: esquerda árvore, meio edição + texto, direita preview
        big_split = QSplitter(Qt.Horizontal, self)

        # Árvore
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(["Tag", "Value"])
        self.tree.itemClicked.connect(self.on_tree_click)
        big_split.addWidget(self.tree)

        # Meio: edição + texto
        mid_widget = QWidget(); mid_layout = QVBoxLayout(mid_widget)
        self.edit_tag = QLineEdit()
        self.edit_value = QLineEdit()
        form = QFormLayout(); form.addRow("Tag:", self.edit_tag); form.addRow("Value:", self.edit_value)
        self.btn_apply = QPushButton("Aplicar alteração"); self.btn_apply.clicked.connect(self.apply_change)
        mid_layout.addLayout(form)
        mid_layout.addWidget(self.btn_apply)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Edite o OTUI aqui... mudanças atualizam a árvore e o preview automaticamente.")
        mid_layout.addWidget(self.text)
        big_split.addWidget(mid_widget)

        # Direita: preview
        right = QWidget(); rlay = QVBoxLayout(right)
        self.preview_label = QLabel("Pré-visualização de imagem")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(350, 350)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_label.setMouseTracking(True)
        self.preview_label.installEventFilter(self)
        self.preview_label.setFocusPolicy(Qt.StrongFocus)
        rlay.addWidget(self.preview_label)
        self.preview_info = QLabel("Coords: -, - | Offset: 0,0 | Elemento: nenhum")
        rlay.addWidget(self.preview_info)
        btns = QHBoxLayout()
        for text, dx, dy in [("↑", 0, -1), ("↓", 0, 1), ("←", -1, 0), ("→", 1, 0)]:
            b = QPushButton(text)
            b.clicked.connect(lambda _, dx=dx, dy=dy: self.shift_image(dx, dy))
            btns.addWidget(b)
        rlay.addLayout(btns)
        big_split.addWidget(right)

        container = QWidget(); lay = QVBoxLayout(container)
        lay.addWidget(big_split)
        self.setCentralWidget(container)

    def _setup_live_parse(self):
        self.parse_timer = QTimer(self)
        self.parse_timer.setSingleShot(True)
        self.parse_timer.setInterval(400)  # debounce
        self.text.textChanged.connect(lambda: self.parse_timer.start())
        self.parse_timer.timeout.connect(self._reparse_from_text)

    # ---------- Arquivo ----------
    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Abrir .otui", "", "OTUI (*.otui);;Todos (*.*)")
        if not path: return
        fp = Path(path)
        try:
            txt = fp.read_text(encoding="utf-8")
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Falha ao ler: {e}"); return
        self.current_file = fp
        # backup
        try: shutil.copy(fp, fp.with_suffix(fp.suffix + ".bak"))
        except Exception: pass
        self.text.blockSignals(True)
        self.text.setPlainText(txt)
        self.text.blockSignals(False)
        # parse inicial
        self._reparse_from_text(push_history=True)
        # tentar descobrir base de imagens
        self.images_base = discover_images_base(fp, self.current_root) if self.current_root else None
        if self.images_base: self.status.showMessage(f"Pasta de imagens: {self.images_base}", 6000)
        else: self.status.showMessage("Defina/descubra a pasta de imagens para o preview.", 6000)

    def save_file(self):
        if not self.current_file:
            QMessageBox.information(self, "Info", "Nenhum arquivo aberto."); return
        try:
            self.current_file.write_text(self.text.toPlainText(), encoding="utf-8")
            self.status.showMessage("Salvo.", 4000)
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Falha ao salvar: {e}")

    def save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Salvar como", "", "OTUI (*.otui)")
        if not path: return
        try:
            Path(path).write_text(self.text.toPlainText(), encoding="utf-8")
            self.status.showMessage(f"Salvo em: {path}", 6000)
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Falha ao salvar: {e}")

    # ---------- Parse & Sync ----------
    def _reparse_from_text(self, push_history: bool = False):
        txt = self.text.toPlainText()
        try:
            root = self.parser.parse_text(txt)
        except Exception as e:
            self.status.showMessage(f"Erro de parse: {e}", 6000)
            return
        self.current_root = root
        self._populate_tree(root)
        if push_history:
            if not self.undo_stack or self.undo_stack[-1] != txt:
                self.undo_stack.append(txt)
                self.redo_stack.clear()
        # atualizar preview se possível
        self.update_preview_from_selection()

    def _populate_tree(self, root: OTUINode):
        self.tree.clear()
        top = QTreeWidgetItem(["Root", ""])
        self.tree.addTopLevelItem(top)
        def add(pitem, node):
            for ch in node.children:
                it = QTreeWidgetItem([ch.tag, ch.value])
                pitem.addChild(it)
                add(it, ch)
        add(top, root)
        self.tree.expandAll()

    def on_tree_click(self, item: QTreeWidgetItem, col: int):
        self.edit_tag.setText(item.text(0))
        self.edit_value.setText(item.text(1))
        self.update_preview_from_selection()

    def apply_change(self):
        it = self.tree.currentItem()
        if not it: return
        # empurra estado atual para undo
        cur_txt = self.text.toPlainText()
        if not self.undo_stack or self.undo_stack[-1] != cur_txt:
            self.undo_stack.append(cur_txt)
            self.redo_stack.clear()
        # aplica no item
        it.setText(0, self.edit_tag.text())
        it.setText(1, self.edit_value.text())
        # regenerar texto a partir da árvore
        new_root = self._root_from_tree_widget()
        new_txt = self.parser.to_string(new_root)
        # atualizar editor de texto (sem retrigger contínuo)
        self.text.blockSignals(True)
        self.text.setPlainText(new_txt)
        self.text.blockSignals(False)
        # salvar como estado atual também
        self.current_root = new_root
        self.update_preview_from_selection()

    def _root_from_tree_widget(self) -> OTUINode:
        def rec(item: QTreeWidgetItem, depth: int) -> OTUINode:
            node = OTUINode(item.text(0), item.text(1), depth, [])
            for i in range(item.childCount()):
                node.children.append(rec(item.child(i), depth+1))
            return node
        root_item = self.tree.topLevelItem(0)
        root = OTUINode("Root", "", -1, [])
        if root_item:
            for i in range(root_item.childCount()):
                root.children.append(rec(root_item.child(i), 0))
        return root

    # ---------- Preview ----------
    def update_preview_from_selection(self):
        it = self.tree.currentItem()
        if it is not self._last_preview_item:
            self.offset_x = 0
            self.offset_y = 0
            self._last_preview_item = it
        if not it:
            self.preview_label.setText("Pré-visualização de imagem"); self.preview_label.setPixmap(QPixmap())
            self._update_preview_info()
            return
        tag = it.text(0).strip().lower()
        val = it.text(1).strip()
        # se for image-source ou parecer caminho de imagem
        if tag == "image-source" or "images" in val.lower() or any(val.lower().endswith(e) for e in IMG_EXTS):
            self._preview_image(val)
        else:
            self.preview_label.setText("Pré-visualização de imagem"); self.preview_label.setPixmap(QPixmap())
        self._update_preview_info()

    def _preview_image(self, val: str):
        if not val:
            self.preview_label.setText("Sem valor para imagem"); self.preview_label.setPixmap(QPixmap()); return
        v = val.strip().strip("'").strip('"')
        otui_dir = self.current_file.parent if self.current_file else Path.cwd()
        full = resolve_image(self.images_base, otui_dir, v)
        if full and full.exists():
            pix = QPixmap(str(full))
            if not pix.isNull():
                scaled = pix.scaled(self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                if self.offset_x or self.offset_y:
                    shifted = QPixmap(scaled.size())
                    shifted.fill(Qt.transparent)
                    painter = QPainter(shifted)
                    painter.drawPixmap(self.offset_x, self.offset_y, scaled)
                    painter.end()
                    scaled = shifted
                self.preview_label.setPixmap(scaled)
                self.preview_label.setToolTip(str(full))
                return
        self.preview_label.setText("Imagem não encontrada"); self.preview_label.setPixmap(QPixmap())

    def shift_image(self, dx: int, dy: int):
        self.offset_x += dx
        self.offset_y += dy
        self.update_preview_from_selection()

    def _update_preview_info(self):
        x, y = self.last_coords
        coord = f"{x}, {y}" if x >= 0 and y >= 0 else "-, -"
        it = self.tree.currentItem()
        elem = it.text(0) if it else "nenhum"
        self.preview_info.setText(f"Coords: {coord} | Offset: {self.offset_x},{self.offset_y} | Elemento: {elem}")

    def eventFilter(self, obj, event):
        if obj is self.preview_label:
            if event.type() == QEvent.MouseMove:
                p = event.position()
                self.last_coords = (int(p.x()), int(p.y()))
                self._update_preview_info()
            elif event.type() == QEvent.KeyPress:
                key = event.key()
                if key == Qt.Key_Left:
                    self.shift_image(-1, 0); return True
                elif key == Qt.Key_Right:
                    self.shift_image(1, 0); return True
                elif key == Qt.Key_Up:
                    self.shift_image(0, -1); return True
                elif key == Qt.Key_Down:
                    self.shift_image(0, 1); return True
        return super().eventFilter(obj, event)

    # ---------- Undo/Redo ----------
    def undo(self):
        if not self.undo_stack: return
        cur = self.text.toPlainText()
        self.redo_stack.append(cur)
        txt = self.undo_stack.pop()
        self.text.blockSignals(True)
        self.text.setPlainText(txt)
        self.text.blockSignals(False)
        self._reparse_from_text(push_history=False)
        self.status.showMessage("Desfeito.", 3000)

    def redo(self):
        if not self.redo_stack: return
        cur = self.text.toPlainText()
        self.undo_stack.append(cur)
        txt = self.redo_stack.pop()
        self.text.blockSignals(True)
        self.text.setPlainText(txt)
        self.text.blockSignals(False)
        self._reparse_from_text(push_history=False)
        self.status.showMessage("Refeito.", 3000)

    # ---------- Pasta de imagens ----------
    def choose_images_base(self):
        d = QFileDialog.getExistingDirectory(self, "Selecione a pasta base das imagens")
        if d:
            self.images_base = Path(d)
            self.status.showMessage(f"Pasta de imagens: {self.images_base}", 5000)
            self.update_preview_from_selection()

    def auto_discover_images(self):
        if not self.current_file or not self.current_root:
            QMessageBox.information(self, "Info", "Abra um .otui primeiro.")
            return
        base = discover_images_base(self.current_file, self.current_root)
        if base:
            self.images_base = base
            QMessageBox.information(self, "Encontrado", f"Pasta de imagens: {self.images_base}")
            self.update_preview_from_selection()
        else:
            QMessageBox.warning(self, "Atenção", "Não foi possível descobrir. Defina manualmente.")

# ==============
# Run
# ==============
if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = OTUIEditor()
    w.show()
    sys.exit(app.exec())
