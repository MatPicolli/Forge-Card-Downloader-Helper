"""
Set Code Mapper
Bidirectional mapping between Scryfall set codes and Forge image folder names.

Strategy:
1. Parse Forge's res/editions/*.txt (if available) — authoritative
2. Fall back to hardcoded dictionary of known discrepancies
3. Default: uppercase Scryfall code
"""

import logging

log = logging.getLogger("ForgeDownloader")

KNOWN_SCRYFALL_TO_FORGE_FOLDER = {
    "lea": "LEA", "leb": "LEB",
    "2ed": "U", "3ed": "RV", "4ed": "4E", "5ed": "5E",
    "6ed": "6E", "7ed": "7E", "8ed": "8E", "9ed": "9E", "10e": "10E",
    "arn": "AN", "atq": "AQ", "leg": "LG", "drk": "DK",
    "fem": "FE", "hml": "HM", "ice": "IA", "all": "AL", "csp": "CS",
    "mir": "MI", "vis": "VI", "wth": "WL",
    "tmp": "TE", "sth": "SH", "exo": "EX",
    "usg": "US", "ulg": "UL", "uds": "UD",
    "mmq": "MM", "nem": "NE", "pcy": "PR",
    "inv": "IN", "pls": "PS", "apc": "AP",
    "ody": "OD", "tor": "TOR", "jud": "JU",
    "ons": "ON", "lgn": "LE", "scg": "SC",
    "me1": "MED", "me2": "ME2", "me3": "ME3", "me4": "ME4",
    "cmd": "CMD", "cm1": "CM1",
    "c13": "C13", "c14": "C14", "c15": "C15", "c16": "C16",
    "c17": "C17", "c18": "C18", "c19": "C19", "c20": "C20", "c21": "C21",
    "por": "PO", "p02": "PO2", "ptk": "PK",
    "s99": "S99", "s00": "S00", "chr": "CH",
}

KNOWN_FORGE_FOLDER_TO_SCRYFALL = {v.upper(): k for k, v in KNOWN_SCRYFALL_TO_FORGE_FOLDER.items()}


class SetCodeMapper:
    def __init__(self):
        self._scryfall_to_folder: dict[str, str] = {}
        self._folder_to_scryfall: dict[str, str] = {}

    def load_forge_mappings(self, parser):
        from forge_edition_parser import ForgeEditionParser
        if not isinstance(parser, ForgeEditionParser):
            return
        for code, edition in parser.editions.items():
            folder = edition.folder_code
            scryfall = edition.effective_scryfall_code
            if scryfall:
                self._scryfall_to_folder[scryfall.lower()] = folder
                self._folder_to_scryfall[folder.upper()] = scryfall.lower()
        log.info(f"Loaded {len(self._scryfall_to_folder)} Forge edition mappings")

    def scryfall_to_forge_folder(self, scryfall_code: str) -> str:
        sc = scryfall_code.lower()
        if sc in self._scryfall_to_folder:
            return self._scryfall_to_folder[sc]
        if sc in KNOWN_SCRYFALL_TO_FORGE_FOLDER:
            return KNOWN_SCRYFALL_TO_FORGE_FOLDER[sc]
        return scryfall_code.upper()

    def forge_folder_to_scryfall(self, forge_folder: str) -> str:
        fu = forge_folder.upper()
        if fu in self._folder_to_scryfall:
            return self._folder_to_scryfall[fu]
        if fu in KNOWN_FORGE_FOLDER_TO_SCRYFALL:
            return KNOWN_FORGE_FOLDER_TO_SCRYFALL[fu]
        return forge_folder.lower()
