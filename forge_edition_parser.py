"""
Forge Edition Parser
Parses .txt files in Forge's res/editions/ directory to extract
set code mappings (Code, Code2, ScryfallCode, Alias).
"""

import logging
from pathlib import Path
from dataclasses import dataclass

log = logging.getLogger("ForgeDownloader")


@dataclass
class ForgeEdition:
    code: str = ""
    code2: str = ""
    name: str = ""
    scryfall_code: str = ""
    alias: str = ""
    set_type: str = ""
    date: str = ""
    card_count: int = 0
    file_path: str = ""

    @property
    def folder_code(self) -> str:
        """Image folder uses Code2 if present, else Code."""
        return self.code2 if self.code2 else self.code

    @property
    def effective_scryfall_code(self) -> str:
        return self.scryfall_code if self.scryfall_code else self.code.lower()


class ForgeEditionParser:
    def __init__(self, editions_path: Path):
        self.editions_path = Path(editions_path)
        self.editions: dict[str, ForgeEdition] = {}
        self._parse_all()

    def _parse_all(self):
        if not self.editions_path.exists():
            log.warning(f"Editions path not found: {self.editions_path}")
            return

        txt_files = list(self.editions_path.glob("*.txt"))
        log.info(f"Found {len(txt_files)} edition files in {self.editions_path}")

        for fpath in txt_files:
            try:
                edition = self._parse_file(fpath)
                if edition and edition.code:
                    self.editions[edition.code.upper()] = edition
            except Exception as e:
                log.debug(f"Could not parse {fpath.name}: {e}")

        log.info(f"Successfully parsed {len(self.editions)} editions")

    def _parse_file(self, fpath: Path) -> ForgeEdition:
        edition = ForgeEdition(file_path=str(fpath))
        in_metadata = False
        in_cards = False
        card_count = 0

        content = None
        for encoding in ["utf-8", "latin-1", "cp1252"]:
            try:
                content = fpath.read_text(encoding=encoding)
                break
            except (UnicodeDecodeError, ValueError):
                continue

        if content is None:
            return edition

        for line in content.splitlines():
            line = line.strip()
            if line.lower() == "[metadata]":
                in_metadata, in_cards = True, False
                continue
            elif line.lower() == "[cards]":
                in_metadata, in_cards = False, True
                continue
            elif line.startswith("["):
                in_metadata, in_cards = False, False
                continue

            if in_metadata and "=" in line:
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key == "Code":       edition.code = value
                elif key == "Code2":    edition.code2 = value
                elif key == "Name":     edition.name = value
                elif key == "ScryfallCode": edition.scryfall_code = value
                elif key == "Alias":    edition.alias = value
                elif key == "Type":     edition.set_type = value
                elif key == "Date":     edition.date = value
            elif in_cards and line:
                card_count += 1

        edition.card_count = card_count
        return edition

    def get_by_code(self, code: str):
        return self.editions.get(code.upper())

    def get_by_scryfall_code(self, scryfall_code: str):
        sc = scryfall_code.lower()
        for ed in self.editions.values():
            if ed.scryfall_code.lower() == sc or ed.code.lower() == sc:
                return ed
        return None
