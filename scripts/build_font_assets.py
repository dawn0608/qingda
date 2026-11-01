"""按已核验字体来源生成全站中文界面分片；FontTools仅用于构建。"""

import argparse
import hashlib
import io
import json
import re
from pathlib import Path
from urllib.request import urlopen

import fontTools
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "frontend/src/assets/fonts"


def ranges(codes):
    spans = []
    for code in sorted(codes):
        if spans and code == spans[-1][1] + 1:
            spans[-1][1] = code
        else:
            spans.append([code, code])
    return ",".join(f"U+{start:X}" + (f"-{end:X}" if start != end else "") for start, end in spans)


def save_subset(raw, codes, filename):
    font = TTFont(io.BytesIO(raw), recalcTimestamp=False)
    subset = Subsetter(Options())
    subset.populate(unicodes=codes)
    subset.subset(font)
    font = instantiateVariableFont(font, {"wght": (400, 600)}, inplace=True)
    font.flavor = "woff2"
    target = DEST / filename
    font.save(target)
    covered = set(font.getBestCmap())
    face = (
        '@font-face {\n  font-family: "Noto Sans SC";\n  font-style: normal;\n'
        "  font-weight: 400 600;\n  font-display: swap;\n"
        f'  src: url("./{filename}") format("woff2");\n'
        f"  unicode-range: {ranges(covered)};\n}}\n"
    )
    return face, dict(
        output=filename,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        bytes=target.stat().st_size,
        glyphs=len(covered),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="可选的已下载Noto Sans SC原字体")
    args = parser.parse_args()
    metadata_file = DEST / "sources.json"
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    source = metadata["noto-sans-sc"]
    if args.source:
        raw = args.source.read_bytes()
    else:
        with urlopen(source["url"], timeout=90) as response:
            raw = response.read()
    if hashlib.sha256(raw).hexdigest() != source["original_sha256"]:
        raise ValueError("字体原件哈希与已核验版本不一致")
    for output in metadata["inter"]["outputs"]:
        if hashlib.sha256((DEST / output["output"]).read_bytes()).hexdigest() != output["sha256"]:
            raise ValueError("既有Inter分片哈希不一致")
    css_file = DEST / "fonts.css"
    inter_faces = [
        face
        for face in re.findall(r"@font-face\s*\{[^}]+\}", css_file.read_text(encoding="utf-8"))
        if 'font-family: "Inter"' in face
    ]
    if len(inter_faces) != 1:
        raise ValueError("Inter字体声明缺失或重复")
    inputs = sorted(
        path
        for path in (ROOT / "frontend/src").rglob("*")
        if path.suffix in {".ts", ".tsx", ".mjs", ".css"}
        and "assets" not in path.relative_to(ROOT / "frontend/src").parts
        and ".test." not in path.name
    )
    inputs.append(ROOT / "prompts/assistant_default.md")
    chars = set("…–—·") | {chr(code) for code in range(0x20, 0x7F)}
    for path in inputs:
        chars.update(path.read_text(encoding="utf-8"))
    core = {ord(char) for char in chars}
    all_chars = set(core)
    for high in range(0xA1, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                all_chars.update(map(ord, bytes([high, low]).decode("gb2312")))
            except UnicodeDecodeError:
                pass
    all_chars.update(range(0x3000, 0x3040))
    all_chars.update(range(0xFF00, 0xFFF0))
    cmap = TTFont(io.BytesIO(raw)).getBestCmap()
    if any(0x3400 <= code <= 0x9FFF and code not in cmap for code in core):
        raise ValueError("现有字体未覆盖界面中文字")
    faces, outputs = [], []
    for codes, filename in (
        (core, "noto-sans-sc-ui.woff2"),
        (all_chars - core, "noto-sans-sc-extended.woff2"),
    ):
        face, output = save_subset(raw, codes, filename)
        faces.append(face)
        outputs.append(output)
    source.update(
        outputs=outputs,
        generator="FontTools " + fontTools.__version__,
        ui_inputs=[path.relative_to(ROOT).as_posix() for path in inputs],
    )
    css_file.write_text(
        "/* 全站按字符范围加载本地字体；中文分片互不重叠。 */\n" + "\n".join(faces + inter_faces) + "\n",
        encoding="utf-8",
    )
    metadata_file.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(font_tools=fontTools.__version__, outputs=outputs)))


if __name__ == "__main__":
    main()
