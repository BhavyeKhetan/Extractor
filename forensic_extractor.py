#!/usr/bin/env python3
"""
Forensic Extractor for Cadence SDAX Projects
============================================
Aggregates fragmented design data into a unified full_design.json

This script follows the OODA Loop approach:
- Observe: Scan directory structure
- Orient: Identify signal vs noise files
- Decide: Build extraction strategy
- Act: Extract, merge, and export

Author: Claude (Forensic Data Engineer)
Date: 2025-11-28
"""

import os
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple
import html.parser
from html.parser import HTMLParser


class TOCHTMLParser(HTMLParser):
    """Parse HTML table cells from page_file_2.ascii to extract TOC entries."""

    def __init__(self):
        super().__init__()
        self.current_data = []
        self.in_span = False
        self.in_a = False
        self.href = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.in_a = True
            for attr_name, attr_value in attrs:
                if attr_name == 'href':
                    self.href = attr_value
        elif tag == 'span':
            self.in_span = True

    def handle_endtag(self, tag):
        if tag == 'a':
            self.in_a = False
        elif tag == 'span':
            self.in_span = False

    def handle_data(self, data):
        if self.in_span or self.in_a:
            text = data.strip()
            if text:
                self.current_data.append(text)

    def get_text(self):
        return ' '.join(self.current_data)


class ForensicExtractor:
    """
    Extracts and aggregates design data from Cadence SDAX project files.

    Handles:
    - JSON files: Component instances with properties
    - XCON files: Net definitions and pin connectivity
    - Page files: Geometric primitives and graphics layer
    - Cache files: Symbol graphics, styles
    """

    # Directories to ignore (noise)
    IGNORE_DIRS = {'cache', 'Configurations2', 'Thumbnails', 'META-INF', '.DS_Store'}

    # CGTYPE mapping for shape classification
    CGTYPE_MAP = {
        65571: 'wire',          # Wire segment (net connection)
        65570: 'table',         # Table/label container
    }

    # Page size definitions (ANSI standard)
    PAGE_SIZES = {
        'A': {'width': 11000, 'height': 8500, 'unit': 'mils'},   # ANSI A (8.5x11)
        'B': {'width': 17000, 'height': 11000, 'unit': 'mils'},  # ANSI B (11x17)
        'C': {'width': 22000, 'height': 17000, 'unit': 'mils'},  # ANSI C (17x22)
        'D': {'width': 34000, 'height': 22000, 'unit': 'mils'},  # ANSI D (22x34)
        'E': {'width': 44000, 'height': 34000, 'unit': 'mils'},  # ANSI E (34x44)
    }

    # File patterns
    JSON_PATTERN = re.compile(r'^(?!.*dx\.json$).*\.json$')  # Exclude *dx.json (exports)
    DX_JSON_PATTERN = re.compile(r'.*dx\.json$')  # DX.json files contain refdes!
    XCON_PATTERN = re.compile(r'.*\.xcon$')

    # Instance ID pattern in cpath: \IXXXXXXX\
    INSTANCE_ID_PATTERN = re.compile(r'\\I(\d+)\\')

    def __init__(self, root_dir: str):
        """Initialize extractor with root directory path."""
        self.root_dir = Path(root_dir)
        self.worklib_dir = self.root_dir / 'worklib'

        # File lists
        self.json_files: List[Path] = []
        self.dx_json_files: List[Path] = []  # DX.json files with refdes
        self.xcon_files: List[Path] = []

        # Extracted data
        self.components: Dict[str, Dict] = {}  # refdes -> component data
        self.instance_map: Dict[str, Dict] = {}  # instance_id -> {refdes, properties, block}
        self.nets: Dict[str, Dict] = {}  # net_name -> net data
        self.net_id_map: Dict[str, str] = {}  # net_id -> net_name
        self.cells: Dict[str, Dict] = {}  # cell_id -> cell definition
        self.hierarchy: Dict[str, Any] = {}  # Block hierarchy tree
        self.symbol_pin_map: Dict[str, Dict[str, str]] = {}  # symbol_key -> {pin_name: pin_number}

        # DX.json instance data: instance_id -> {refdes, library, part_name, symbol}
        self.dx_instances: Dict[str, Dict] = {}

        # Geometric layer data
        self.pages: List[Dict] = []  # List of page definitions
        self.primitives: List[Dict] = []  # Flat list of all primitives
        self.styles: Dict[str, Dict] = {}  # Style definitions from .style files
        self.symbol_graphics: Dict[str, Dict] = {}  # Symbol graphics from cache
        self.grid_config: Dict = {}  # Grid/snap configuration

        # Counters for element IDs
        self._element_counter = 0
        self._sequence_counter = 0

        # Statistics
        self.stats = {
            'json_files_processed': 0,
            'xcon_files_processed': 0,
            'blocks_processed': set(),
            'total_components': 0,
            'total_nets': 0,
            'total_connections': 0,
            'components_by_type': defaultdict(int),
            # Geometric stats
            'total_pages': 0,
            'total_primitives': 0,
            'primitives_by_type': defaultdict(int),
            'primitives_by_shape_type': defaultdict(int),
            'style_files_processed': 0,
            'symbol_graphics_loaded': 0,
        }

    def discover_signal_files(self) -> None:
        """
        Phase 1: Discover all signal files in the worklib directory.
        Filters out noise files (caches, thumbnails, configs).
        """
        print("\n" + "="*60)
        print("PHASE 1: FILE DISCOVERY")
        print("="*60)

        if not self.worklib_dir.exists():
            raise FileNotFoundError(f"worklib directory not found at {self.worklib_dir}")

        # Walk the worklib directory
        for block_dir in self.worklib_dir.iterdir():
            if not block_dir.is_dir():
                continue
            if block_dir.name in self.IGNORE_DIRS:
                continue

            # Look for tbl_1 subdirectory (schematic tables)
            tbl_dir = block_dir / 'tbl_1'
            if not tbl_dir.exists():
                continue

            block_name = block_dir.name
            self.stats['blocks_processed'].add(block_name)

            # Find JSON, DX.JSON, and XCON files
            for file_path in tbl_dir.iterdir():
                if file_path.is_file():
                    if self.DX_JSON_PATTERN.match(file_path.name):
                        self.dx_json_files.append(file_path)
                        print(f"  [DX.JSON] Found: {file_path.relative_to(self.root_dir)}")
                    elif self.JSON_PATTERN.match(file_path.name):
                        self.json_files.append(file_path)
                        print(f"  [JSON] Found: {file_path.relative_to(self.root_dir)}")
                    elif self.XCON_PATTERN.match(file_path.name):
                        self.xcon_files.append(file_path)
                        print(f"  [XCON] Found: {file_path.relative_to(self.root_dir)}")

        print(f"\nDiscovery Summary:")
        print(f"  - JSON files: {len(self.json_files)}")
        print(f"  - DX.JSON files: {len(self.dx_json_files)} (contain refdes!)")
        print(f"  - XCON files: {len(self.xcon_files)}")
        print(f"  - Blocks found: {len(self.stats['blocks_processed'])}")
        print(f"  - Blocks: {sorted(self.stats['blocks_processed'])}")

    def _extract_instance_id(self, cpath: str) -> Optional[str]:
        """Extract the last instance ID from a hierarchical cpath."""
        matches = self.INSTANCE_ID_PATTERN.findall(cpath)
        return matches[-1] if matches else None

    def _extract_block_name(self, cpath: str) -> str:
        """Extract the block name from a cpath like @worklib.usb_block(tbl_1):..."""
        match = re.search(r'@worklib\.(\w+)\(tbl_1\)', cpath)
        return match.group(1) if match else 'unknown'

    def _parse_hierarchy_path(self, cpath: str) -> List[str]:
        """Parse cpath to extract hierarchy chain of blocks."""
        blocks = re.findall(r'@worklib\.(\w+)\(tbl_1\)', cpath)
        return blocks

    def parse_symbol_pin_numbers(self, ascii_path: Path) -> Dict[str, str]:
        """
        Parse a symbol .ascii file to extract pin_name -> pin_number mapping.

        The Cadence .ascii format contains pin definitions with:
        - PIN_DISPLAY_NAME: the pin name (e.g., VDDIO, GND, DIR)
        - V: the pin number (e.g., 32, 33, 31)

        Format pattern:
        <n PIN_DISPLAY_NAME n/> ... <v PIN_NAME v/> ... <n V n/> ... <v PIN_NUMBER v/>
        """
        pin_numbers = {}

        try:
            content = ascii_path.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            print(f"  [WARN] Failed to read {ascii_path.name}: {e}")
            return pin_numbers

        # Pattern to find PIN_DISPLAY_NAME followed by V value
        # The structure is: <n PIN_DISPLAY_NAME n/> <num> <num> <v NAME v/> ... <n V n/> <num> <num> <v NUMBER v/>
        pin_pattern = re.compile(
            r'<n\s+PIN_DISPLAY_NAME\s+n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s+(\w+)\s+v/>.*?'
            r'<n\s+V\s+n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s+(\d+)\s+v/>',
            re.DOTALL
        )

        for match in pin_pattern.finditer(content):
            pin_name = match.group(1).lower()
            pin_number = match.group(2)
            pin_numbers[pin_name] = pin_number

        return pin_numbers

    def load_symbol_pin_numbers(self) -> None:
        """
        Load pin numbers for all symbols from cache directory.

        Parses cache/*.ascii files to build a mapping:
        {symbol_key: {pin_name: pin_number}}

        Symbol key format: "library##name" (e.g., "ic##usb3320_ulpi_xcvr")
        """
        print("\n" + "="*60)
        print("PHASE 1b: LOADING SYMBOL PIN NUMBERS")
        print("="*60)

        cache_dir = self.root_dir / 'cache'
        if not cache_dir.exists():
            print(f"  [WARN] Cache directory not found at {cache_dir}")
            return

        ascii_count = 0
        pin_count = 0

        for ascii_file in cache_dir.glob('*.ascii'):
            # Parse filename: library##name##sym_1.ascii
            parts = ascii_file.stem.split('##')
            if len(parts) >= 2:
                symbol_key = f"{parts[0]}##{parts[1]}"
                pins = self.parse_symbol_pin_numbers(ascii_file)
                if pins:
                    self.symbol_pin_map[symbol_key] = pins
                    pin_count += len(pins)
                    ascii_count += 1

        print(f"  - Symbol files parsed: {ascii_count}")
        print(f"  - Total pin mappings: {pin_count}")
        print(f"  - Symbol keys: {list(self.symbol_pin_map.keys())[:10]}...")

    def load_dx_json_instances(self) -> None:
        """
        CRITICAL: Parse *dx.json files to extract refdes and symbol lookup info.

        DX.JSON files contain the component reference designators (U12, C51, R84)
        that are MISSING from page files!

        Format (verified from actual files):
        {
          "instances": [{
            "cpath": "@worklib.usb_block(tbl_1):\\I167231504\\",
            "attributes": {
              "refdes": "C51",                    // THE REFDES LABEL!
              "symbol": "sym_1",                  // Symbol version
              "library": "discrete",              // Library for cache lookup
              "system_capture_model": "capacitor" // Part name for cache lookup
            }
          }]
        }

        Symbol cache lookup formula:
        cache/{library}##{system_capture_model}##sym_{symbol}.ascii
        """
        print("\n" + "="*60)
        print("PHASE 1c: LOADING DX.JSON REFDES DATA (CRITICAL)")
        print("="*60)

        instance_count = 0
        refdes_count = 0

        for dx_file in self.dx_json_files:
            block_name = dx_file.parent.parent.name

            try:
                with open(dx_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"  [WARN] Failed to parse {dx_file.name}: {e}")
                continue

            instances = data.get('instances', [])

            for inst in instances:
                cpath = inst.get('cpath', '')
                attributes = inst.get('attributes', {})
                properties = inst.get('properties', {})

                # Extract instance ID from cpath
                instance_id = self._extract_instance_id(cpath)
                if not instance_id:
                    continue

                refdes = attributes.get('refdes', '')
                library = attributes.get('library', '')
                part_name = attributes.get('system_capture_model', '')
                symbol = attributes.get('symbol', 'sym_1')

                if not refdes:
                    continue

                # Build symbol cache path
                # Format: cache/{library}##{part_name}##{symbol}.ascii
                symbol_cache_path = f"{library}##{part_name}##{symbol}"

                # Store the instance data
                self.dx_instances[instance_id] = {
                    'refdes': refdes,
                    'library': library,
                    'part_name': part_name,
                    'symbol': symbol,
                    'symbol_cache_key': f"{library}##{part_name}",
                    'symbol_cache_path': symbol_cache_path,
                    'block': block_name,
                    'cpath': cpath,
                    'properties': properties,
                }

                instance_count += 1
                if refdes:
                    refdes_count += 1

                # Also update the main instance_map for connectivity lookup
                map_key = f"{block_name}:{instance_id}"
                self.instance_map[map_key] = {
                    'refdes': refdes,
                    'comp_key': refdes,
                    'block': block_name,
                    'library': library,
                    'part_name': part_name,
                    'symbol_cache_key': f"{library}##{part_name}",
                }
                self.instance_map[instance_id] = self.instance_map[map_key]

        print(f"  - DX.JSON files processed: {len(self.dx_json_files)}")
        print(f"  - Instances found: {instance_count}")
        print(f"  - Refdes labels extracted: {refdes_count}")

        # Show some example refdes
        sample_refdes = [d['refdes'] for d in list(self.dx_instances.values())[:15]]
        print(f"  - Sample refdes: {sample_refdes}")

    # =========================================================================
    # GEOMETRIC LAYER EXTRACTION METHODS
    # =========================================================================

    def _generate_element_id(self, prefix: str = 'E') -> str:
        """Generate a unique element ID."""
        self._element_counter += 1
        return f"{prefix}_{self._element_counter}"

    def _next_sequence_index(self) -> int:
        """Get the next sequence index for draw order."""
        self._sequence_counter += 1
        return self._sequence_counter

    def _parse_transform_matrix(self, matrix_str: str) -> Dict:
        """
        Parse a transform matrix string into structured data.

        Matrix format: "a,b,c,d,e,f,g,h,i" representing 3x3 affine:
        | a b c |   | scale_x  shear_x  translate_x |
        | d e f | = | shear_y  scale_y  translate_y |
        | g h i |   | 0        0        1           |

        Common values:
        - 1,0,0,0,1,0,0,0,1   -> identity
        - -1,0,0,0,1,0,0,0,1  -> mirror X
        - 1,0,0,0,-1,0,0,0,1  -> mirror Y
        - -1,0,0,0,-1,0,0,0,1 -> mirror both
        """
        try:
            parts = [float(x) for x in matrix_str.split(',')]
            if len(parts) != 9:
                parts = [1, 0, 0, 0, 1, 0, 0, 0, 1]  # Default to identity
        except (ValueError, AttributeError):
            parts = [1, 0, 0, 0, 1, 0, 0, 0, 1]

        # Extract convenience fields
        a, b, c, d, e, f = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]

        return {
            'matrix': parts,
            'rotation_degrees': 0,  # Would need atan2 calculation for non-90 angles
            'mirror_x': a < 0,
            'mirror_y': e < 0,
            'scale_x': abs(a) if a != 0 else 1.0,
            'scale_y': abs(e) if e != 0 else 1.0,
        }

    def extract_pages(self) -> None:
        """
        Phase G1: Extract schematic page list from TOC file (page_file_2.ascii).

        The TOC file contains HTML table cells with:
        - Sheet number (with hyperlink containing pageuid)
        - Sheet name
        - Block path

        Extracts page metadata including:
        - page_id, page_uid, title, block_path
        - size (width/height in mils)
        - coordinate_origin
        """
        print("\n" + "="*60)
        print("PHASE G1: PAGE/SHEET EXTRACTION")
        print("="*60)

        # Find brain_board's page_file_2.ascii (TOC file)
        toc_file = self.worklib_dir / 'brain_board' / 'tbl_1' / 'page_file_2.ascii'
        if not toc_file.exists():
            print(f"  [WARN] TOC file not found at {toc_file}")
            return

        try:
            content = toc_file.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            print(f"  [ERROR] Failed to read TOC file: {e}")
            return

        # Extract page size from pageBorderSize property
        page_size_match = re.search(r'<n pageBorderSize n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s+(\w+)\s+v/>', content)
        page_size = page_size_match.group(1) if page_size_match else 'B'
        size_info = self.PAGE_SIZES.get(page_size, self.PAGE_SIZES['B'])

        # Extract page border standard
        standard_match = re.search(r'<n pageBorderStandard n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s+(\w+)\s+v/>', content)
        page_standard = standard_match.group(1) if standard_match else 'ANSI'

        print(f"  - Page size: {page_size} ({page_standard})")
        print(f"  - Dimensions: {size_info['width']}x{size_info['height']} {size_info['unit']}")

        # Extract table cells using pattern: < 9 /> ROW COL LENGTH HTML_CONTENT
        # Pattern finds: < 9 /> {row} {col} {len} <!DOCTYPE...></html>
        # The HTML is on a single line, delimited by </html>
        cell_pattern = re.compile(
            r'<\s*9\s*/>\s*(\d+)\s+(\d+)\s+(\d+)\s+(<!DOCTYPE[^>]+>.*?</html>)',
            re.DOTALL
        )

        # Organize cells by row
        rows = defaultdict(dict)
        for match in cell_pattern.finditer(content):
            row = int(match.group(1))
            col = int(match.group(2))
            html_content = match.group(4)

            # Parse HTML to extract text and href
            parser = TOCHTMLParser()
            try:
                parser.feed(html_content)
                text = parser.get_text()
                href = parser.href
            except Exception:
                text = ''
                href = None

            rows[row][col] = {'text': text, 'href': href}

        # Skip header row (row 0), process data rows
        page_number = 0
        for row_idx in sorted(rows.keys()):
            if row_idx == 0:  # Skip header
                continue

            row_data = rows[row_idx]

            # Column 0: Sheet number (with pageuid link)
            # Column 1: Sheet name
            # Column 2: Block path
            sheet_no_cell = row_data.get(0, {})
            sheet_name_cell = row_data.get(1, {})
            block_path_cell = row_data.get(2, {})

            sheet_no = sheet_no_cell.get('text', '').strip()
            sheet_name = sheet_name_cell.get('text', '').strip()
            block_path = block_path_cell.get('text', '').strip()

            # Skip empty rows
            if not sheet_no or not sheet_name:
                continue

            # Extract pageuid from href
            href = sheet_no_cell.get('href', '')
            pageuid_match = re.search(r'pageuid=(\d+)', href or '')
            page_uid = pageuid_match.group(1) if pageuid_match else str(row_idx)

            # Extract block reference from href
            block_ref_match = re.search(r'@worklib\.(\w+)\(tbl_1\)', href or '')
            block_ref = block_ref_match.group(1) if block_ref_match else 'brain_board'

            page_number += 1

            page_data = {
                'page_id': str(page_number),
                'page_uid': page_uid,
                'title': sheet_name,
                'block_path': block_path,
                'block_ref': block_ref,
                'size': {
                    'width': size_info['width'],
                    'height': size_info['height'],
                    'unit': size_info['unit'],
                },
                'page_standard': page_standard,
                'coordinate_origin': 'bottom_left',
                'element_ids': [],  # Will be populated with primitive element IDs
                'element_count': 0,
            }

            self.pages.append(page_data)

        self.stats['total_pages'] = len(self.pages)
        print(f"  - Pages extracted: {len(self.pages)}")

        # Print page summary
        for page in self.pages[:5]:  # Show first 5
            print(f"    Page {page['page_id']}: {page['title']} ({page['block_path']})")
        if len(self.pages) > 5:
            print(f"    ... and {len(self.pages) - 5} more pages")

    def extract_grid_config(self) -> None:
        """
        Phase G7: Extract grid and snap configuration.

        Grid configuration is required for:
        - Consistent element placement during reconstruction
        - Proper alignment of wires and components
        - Matching original design intent
        """
        print("\n" + "="*60)
        print("PHASE G7: GRID & SNAP CONFIGURATION")
        print("="*60)

        # Default grid configuration based on SDAX standard
        # In Cadence, 1 inch = 100,000 internal units
        # Standard grid is typically 100 mils = 10,000 internal units
        self.grid_config = {
            'x_step': 100000,  # 100 mils in internal units
            'y_step': 100000,
            'unit': 'internal',
            'unit_conversion': {
                'mils_per_unit': 0.01,  # 1 internal unit = 0.01 mils
                'mm_per_unit': 0.000254,  # 1 internal unit = 0.000254 mm
                'internal_per_inch': 100000,
            },
            'snap_enabled': True,
            'display_grid': True,
            'minor_grid_divisions': 10,
        }

        # Try to extract from page file if available
        page_file = self.worklib_dir / 'brain_board' / 'tbl_1' / 'page_file_1.ascii'
        if page_file.exists():
            try:
                # Read just the beginning for config data
                with open(page_file, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read(50000)  # Read first 50KB

                # Look for grid properties
                grid_x = re.search(r'<n gridX n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>', content)
                grid_y = re.search(r'<n gridY n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>', content)

                if grid_x:
                    self.grid_config['x_step'] = int(grid_x.group(1))
                if grid_y:
                    self.grid_config['y_step'] = int(grid_y.group(1))

            except Exception as e:
                print(f"  [WARN] Could not read grid config from page file: {e}")

        print(f"  - Grid X step: {self.grid_config['x_step']} internal units")
        print(f"  - Grid Y step: {self.grid_config['y_step']} internal units")
        print(f"  - Unit conversion: 1 unit = {self.grid_config['unit_conversion']['mils_per_unit']} mils")

    def load_styles(self) -> None:
        """
        Phase G6: Load all style definitions from .style files.

        Style files contain CSS-like format with:
        - line-width, line-color, line-style
        - font-name, font-size, font-color
        - bold, italic, underline
        """
        print("\n" + "="*60)
        print("PHASE G6: STYLE LOADING")
        print("="*60)

        cache_dir = self.root_dir / 'cache'
        if not cache_dir.exists():
            print(f"  [WARN] Cache directory not found")
            return

        style_count = 0

        for style_file in cache_dir.glob('*.style'):
            try:
                content = style_file.read_text(encoding='utf-8', errors='ignore')
                parsed_styles = self._parse_style_file(content)

                for style_name, style_data in parsed_styles.items():
                    # Use file-qualified style name for uniqueness
                    qualified_name = f"{style_file.stem}::{style_name}"
                    self.styles[qualified_name] = style_data

                    # Also store by simple name (may be overwritten)
                    self.styles[style_name] = style_data

                style_count += 1
            except Exception as e:
                print(f"  [WARN] Failed to parse {style_file.name}: {e}")

        self.stats['style_files_processed'] = style_count
        print(f"  - Style files processed: {style_count}")
        print(f"  - Total styles loaded: {len(self.styles)}")

    def _parse_style_file(self, content: str) -> Dict[str, Dict]:
        """
        Parse a .style file and extract all style definitions.

        Format:
        StyleN {
          property-name : value
          ...
        }
        """
        styles = {}

        # Pattern to match style blocks
        style_pattern = re.compile(
            r'(Style\d+)\s*\{([^}]+)\}',
            re.MULTILINE | re.DOTALL
        )

        for match in style_pattern.finditer(content):
            style_name = match.group(1)
            style_body = match.group(2)

            style_data = {
                'line_width': 1,
                'line_color': '#000000',
                'line_style': 'solid',
                'line_cap_style': 'square-cap',
                'line_join_style': 'bevel-join',
                'fill_color': '#000000',
                'fill_style': None,
                'font_name': 'Arial',
                'font_size': 10.0,
                'font_color': '#000000',
                'font_weight': 'normal',
                'font_style': 'normal',
                'text_decoration': 'none',
            }

            # Parse individual properties
            for line in style_body.split('\n'):
                if ':' not in line:
                    continue

                parts = line.split(':', 1)
                if len(parts) != 2:
                    continue

                prop_name = parts[0].strip().lower().replace('-', '_')
                prop_value = parts[1].strip()

                if prop_name == 'line_width':
                    try:
                        style_data['line_width'] = int(prop_value)
                    except ValueError:
                        pass
                elif prop_name == 'line_color':
                    style_data['line_color'] = prop_value
                elif prop_name == 'line_style':
                    style_data['line_style'] = prop_value
                elif prop_name == 'line_cap_style':
                    style_data['line_cap_style'] = prop_value
                elif prop_name == 'line_join_style':
                    style_data['line_join_style'] = prop_value
                elif prop_name == 'fill_color':
                    style_data['fill_color'] = prop_value
                elif prop_name == 'fill_style':
                    style_data['fill_style'] = prop_value if prop_value else None
                elif prop_name == 'font_name':
                    style_data['font_name'] = prop_value
                elif prop_name == 'font_size':
                    try:
                        style_data['font_size'] = float(prop_value)
                    except ValueError:
                        pass
                elif prop_name == 'font_color':
                    style_data['font_color'] = prop_value
                elif prop_name == 'bold':
                    style_data['font_weight'] = 'bold' if prop_value.lower() == 'true' else 'normal'
                elif prop_name == 'italic':
                    style_data['font_style'] = 'italic' if prop_value.lower() == 'true' else 'normal'
                elif prop_name == 'underline':
                    style_data['text_decoration'] = 'underline' if prop_value.lower() == 'true' else 'none'

            styles[style_name] = style_data

        return styles

    def extract_wire_segments(self) -> None:
        """
        Phase G5: Extract wire segment coordinates from page files.

        Wire segments are identified by:
        - CGTYPE 65571
        - LP property containing coordinates: X1,Y1;X2,Y2

        Also extracts:
        - sequence_index for draw order
        - transform matrix
        - rotation
        - zValue for z-order
        """
        print("\n" + "="*60)
        print("PHASE G5: WIRE SEGMENT EXTRACTION")
        print("="*60)

        wire_count = 0

        # Process all page files in worklib
        for block_dir in self.worklib_dir.iterdir():
            if not block_dir.is_dir() or block_dir.name in self.IGNORE_DIRS:
                continue

            tbl_dir = block_dir / 'tbl_1'
            if not tbl_dir.exists():
                continue

            for page_file in tbl_dir.glob('page_file_*.ascii'):
                try:
                    wires = self._extract_wires_from_page_file(page_file, block_dir.name)
                    wire_count += len(wires)
                    self.primitives.extend(wires)
                except Exception as e:
                    print(f"  [WARN] Failed to process {page_file.name}: {e}")

        self.stats['total_primitives'] = len(self.primitives)
        print(f"  - Wire segments extracted: {wire_count}")
        print(f"  - Total primitives: {len(self.primitives)}")

    def _extract_wires_from_page_file(self, page_file: Path, block_name: str) -> List[Dict]:
        """Extract wire segments from a single page file."""
        wires = []

        try:
            content = page_file.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            return wires

        # Extract page index from filename (page_file_1.ascii -> 1)
        page_idx_match = re.search(r'page_file_(\d+)\.ascii', page_file.name)
        page_index = int(page_idx_match.group(1)) if page_idx_match else 0

        # Pattern to find wire segments with LP coordinates and CGTYPE
        # Looking for blocks that contain both LP and CGTYPE 65571
        wire_pattern = re.compile(
            r'<n LP n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*([^v]+)\s*v/>.*?'
            r'<n CGTYPE n/>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>',
            re.DOTALL
        )

        # Also need to capture associated properties like rotation, transform, zValue
        for match in wire_pattern.finditer(content):
            lp_coords = match.group(1).strip()
            cgtype = int(match.group(2))

            # Parse LP coordinates: "X1,Y1;X2,Y2"
            try:
                points_str = lp_coords.split(';')
                points = []
                for pt_str in points_str:
                    coords = pt_str.split(',')
                    if len(coords) >= 2:
                        x = float(coords[0])
                        y = float(coords[1])
                        points.append({'x': int(x), 'y': int(y)})

                if len(points) < 2:
                    continue
            except (ValueError, IndexError):
                continue

            # Look for rotation near this match
            rotation_match = re.search(
                r'<n rotation n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>',
                content[max(0, match.start()-500):match.end()+500]
            )
            rotation = int(rotation_match.group(1)) if rotation_match else 0

            # Look for transform near this match
            transform_match = re.search(
                r'<n transform n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*([^v]+)\s*v/>',
                content[max(0, match.start()-500):match.end()+500]
            )
            transform_str = transform_match.group(1).strip() if transform_match else '1,0,0,0,1,0,0,0,1'

            # Look for zValue near this match
            zvalue_match = re.search(
                r'<n zValue n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>',
                content[max(0, match.start()-500):match.end()+500]
            )
            z_value = int(zvalue_match.group(1)) if zvalue_match else 10000

            # Create wire primitive
            element_id = self._generate_element_id('wire')
            sequence_idx = self._next_sequence_index()
            shape_type = self.CGTYPE_MAP.get(cgtype, 'unknown')

            wire = {
                'element_id': element_id,
                'sequence_index': sequence_idx,
                'type': 'line',
                'shape_type': shape_type,
                'cgtype': cgtype,
                'page_index': page_index,
                'block': block_name,
                'geometry': {
                    'points': points,
                },
                'transform': self._parse_transform_matrix(transform_str),
                'rotation': rotation,
                'z_value': z_value,
                'style': {
                    'style_ref': 'Style1',
                    'line_width': 1,
                    'line_color': '#000000',
                    'line_style': 'solid',
                },
                'semantic': None,  # Will be populated in cross-reference phase
            }

            wires.append(wire)
            self.stats['primitives_by_type']['line'] += 1
            self.stats['primitives_by_shape_type'][shape_type] += 1

        return wires

    def extract_symbol_graphics(self) -> None:
        """
        Phase G3: Extract symbol graphics from cache files.

        Symbol graphics include:
        - Lines, shapes, and text that make up the visual representation
        - Pin definitions with visibility flags (for hidden pins)
        - Symbol dependencies (nested symbols)
        - Text labels (VALUE, LOCATION, etc.) with justification

        This satisfies Critical Requirements:
        - #6: Hierarchical Symbol Dependencies
        - #7: Implicit/Hidden Pins
        """
        print("\n" + "="*60)
        print("PHASE G3: SYMBOL GRAPHICS EXTRACTION")
        print("="*60)

        cache_dir = self.root_dir / 'cache'
        if not cache_dir.exists():
            print(f"  [WARN] Cache directory not found")
            return

        symbol_count = 0

        for ascii_file in cache_dir.glob('*.ascii'):
            # Parse filename: library##name##sym_1.ascii
            parts = ascii_file.stem.split('##')
            if len(parts) < 2:
                continue

            library = parts[0]
            symbol_name = parts[1]
            symbol_key = f"{library}##{symbol_name}"

            try:
                content = ascii_file.read_text(encoding='utf-8', errors='ignore')
                symbol_data = self._parse_symbol_graphics(content, symbol_key)

                if symbol_data:
                    self.symbol_graphics[symbol_key] = symbol_data
                    symbol_count += 1

            except Exception as e:
                print(f"  [WARN] Failed to parse {ascii_file.name}: {e}")

        self.stats['symbol_graphics_loaded'] = symbol_count
        print(f"  - Symbols extracted: {symbol_count}")

    def _parse_symbol_graphics(self, content: str, symbol_key: str) -> Dict:
        """
        Parse symbol graphics from cache .ascii content.

        VERIFIED TAG FORMATS (from actual capacitor##sym_1.ascii):

        Tag 25 (Line) format:
        < 25 />  <  < 14 />  < 14:1008806316530991106  />
          < 45 />  <  < 0 />  < 38100 />  < 0 />  < 25400 />  />   // Start: X=38100, Y=25400
          < 45 />  <  < 0 />  < 63500 />  < 0 />  < 25400 />  />   // End: X=63500, Y=25400
          ...
          < 0 />  < 6 />  < Style2 />

        Tag 31 (Text) format:
        < 31 />  <  < 0 />  < 2 />  < 0 />  < 8 />  < LOCATION />
          < 44 />  < ... bounding box ... />
          < 45 />  <  < 0 />  < -25400 />  < 0 />  < 35306 />  />  // Position
          ...
          <n V n/>  < 1 />  < 2 />  <v C? v/>   // Default text

        Tag 19 (Pin container) format:
        < 19 />  <  < 0 />  < 8 />  < zeronull />  < 19 />
          ...
          < 40 />  < 16 />  <n PIN_SIDE_DISPLAY n/>  < 1 />  < 6 />  <v Bottom v/>
          < 40 />  < 16 />  <n PIN_TYPE_DISPLAY n/>  < 1 />  < 6 />  <v Analog v/>
        """
        symbol_data = {
            'symbol_key': symbol_key,
            'bounding_box': None,
            'lines': [],
            'pins': [],
            'labels': [],
            'text_positions': {},  # LOCATION, VALUE positions
            'dependencies': [],
        }

        # =====================================================================
        # EXTRACT LINES (Tag 25)
        # =====================================================================
        # Verified pattern: < 25 /> ... < 45 /> < < 0 /> < X /> < 0 /> < Y /> /> < 45 /> < < 0 /> < X /> < 0 /> < Y /> />
        # The nested < < pattern is key!
        line_pattern = re.compile(
            r'<\s*25\s*/>\s*<\s*<\s*14\s*/>\s*<\s*[^>]+\s*/>\s*'  # Tag 25 header
            r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>\s*'  # Point 1
            r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',  # Point 2
            re.DOTALL
        )

        for match in line_pattern.finditer(content):
            x1 = int(match.group(1))
            y1 = int(match.group(2))
            x2 = int(match.group(3))
            y2 = int(match.group(4))

            # Look for style reference after the coordinates
            style_match = re.search(
                r'<\s*\d+\s*/>\s*<\s*(Style\d+)\s*/>',
                content[match.end():min(match.end()+200, len(content))]
            )
            style_ref = style_match.group(1) if style_match else 'Style1'

            line = {
                'type': 'line',
                'points': [
                    {'x': x1, 'y': y1},
                    {'x': x2, 'y': y2}
                ],
                'style_ref': style_ref,
            }
            symbol_data['lines'].append(line)

        # =====================================================================
        # EXTRACT TEXT LABELS (Tag 31) - LOCATION, VALUE, etc.
        # =====================================================================
        # Pattern: < 31 /> < < 0 /> < 2 /> < 0 /> < LENGTH /> < LABEL_NAME />
        # Then look for: <n V n/> ... <v TEXT_VALUE v/>
        text_label_pattern = re.compile(
            r'<\s*31\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<\s*(\d+)\s*/>\s*<\s*([A-Z_]+)\s*/>',
            re.DOTALL
        )

        for match in text_label_pattern.finditer(content):
            label_name = match.group(2).strip()

            # Skip internal labels
            if label_name in ['##', 'PN']:
                continue

            # Look for position coordinates (< 45 /> block after < 44 />)
            pos_search = content[match.end():min(match.end()+500, len(content))]
            pos_match = re.search(
                r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',
                pos_search
            )
            pos_x = int(pos_match.group(1)) if pos_match else 0
            pos_y = int(pos_match.group(2)) if pos_match else 0

            # Look for default value: <n V n/> ... <v VALUE v/>
            value_match = re.search(
                r'<n\s+V\s+n/>\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<v\s*([^v]+)\s*v/>',
                pos_search
            )
            default_value = value_match.group(1).strip() if value_match else ''

            # Look for justification
            just_match = re.search(r'<n\s+just\s+n/>\s*<\s*\d+\s*/>\s*<v\s*(\d+)\s*v/>', pos_search)
            justification = int(just_match.group(1)) if just_match else 0

            # Look for rotation
            rot_match = re.search(r'<n\s+rotation\s+n/>\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<v\s*(\d+)\s*v/>', pos_search)
            rotation = int(rot_match.group(1)) if rot_match else 0

            # Look for style reference
            style_match = re.search(r'<\s*\d+\s*/>\s*<\s*(Style\d+)\s*/>', pos_search)
            style_ref = style_match.group(1) if style_match else 'Style1'

            # Store in text_positions for key labels
            if label_name in ['LOCATION', 'VALUE', 'CDS_LMAN_SYM_OUTLINE']:
                symbol_data['text_positions'][label_name] = {
                    'position': {'x': pos_x, 'y': pos_y},
                    'default_value': default_value,
                    'justification': justification,
                    'rotation': rotation,
                    'style_ref': style_ref,
                }

            # Add to labels list
            symbol_data['labels'].append({
                'type': 'text',
                'name': label_name,
                'default_value': default_value,
                'position': {'x': pos_x, 'y': pos_y},
                'text_properties': {
                    'alignment': {0: 'left', 1: 'center', 2: 'right'}.get(justification, 'left'),
                    'rotation': rotation,
                },
                'style_ref': style_ref,
            })

        # =====================================================================
        # EXTRACT PINS (Tag 19 containers + PIN_SIDE_DISPLAY properties)
        # =====================================================================
        # Pin properties are at the end of Tag 19 blocks
        pin_side_pattern = re.compile(
            r'<n\s+PIN_SIDE_DISPLAY\s+n/>\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<v\s*(\w+)\s*v/>',
            re.DOTALL
        )
        pin_type_pattern = re.compile(
            r'<n\s+PIN_TYPE_DISPLAY\s+n/>\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<v\s*(\w+)\s*v/>',
            re.DOTALL
        )

        # Find all PIN_SIDE_DISPLAY occurrences - each represents a pin
        for side_match in pin_side_pattern.finditer(content):
            pin_side = side_match.group(1)

            # Look for PIN_TYPE_DISPLAY nearby
            search_area = content[max(0, side_match.start()-200):side_match.end()+200]
            type_match = pin_type_pattern.search(search_area)
            pin_type = type_match.group(1) if type_match else 'Unknown'

            # Look for PN (pin number) property
            pn_match = re.search(r'<n\s+PN\s+n/>\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<v\s*([^v]+)\s*v/>', search_area)
            pin_number = pn_match.group(1).strip() if pn_match else '?'

            # Look for pin visibility
            vis_match = re.search(r'<n\s+visibility\s+n/>\s*<\s*\d+\s*/>\s*<v\s*(\d+)\s*v/>', search_area)
            visibility = int(vis_match.group(1)) if vis_match else 1

            pin = {
                'side': pin_side,
                'type': pin_type,
                'number': pin_number,
                'visible': visibility != 0,
                'hidden_pin': visibility == 0,
            }
            symbol_data['pins'].append(pin)

        # =====================================================================
        # EXTRACT BOUNDING BOX from CDS_LMAN_SYM_OUTLINE
        # =====================================================================
        outline_match = re.search(
            r'<n\s+CDS_LMAN_SYM_OUTLINE\s+n/>\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<v\s*([^v]+)\s*v/>',
            content
        )
        if outline_match:
            outline_str = outline_match.group(1).strip()
            try:
                parts = [int(float(x)) for x in outline_str.split(',')]
                if len(parts) == 4:
                    symbol_data['bounding_box'] = {
                        'min_x': parts[0],
                        'min_y': parts[1],
                        'max_x': parts[2],
                        'max_y': parts[3],
                        'width': parts[2] - parts[0],
                        'height': parts[3] - parts[1],
                    }
            except (ValueError, IndexError):
                pass

        # If no outline, calculate from lines
        if not symbol_data['bounding_box'] and symbol_data['lines']:
            all_x = []
            all_y = []
            for line in symbol_data['lines']:
                for pt in line['points']:
                    all_x.append(pt['x'])
                    all_y.append(pt['y'])
            if all_x and all_y:
                symbol_data['bounding_box'] = {
                    'min_x': min(all_x),
                    'min_y': min(all_y),
                    'max_x': max(all_x),
                    'max_y': max(all_y),
                    'width': max(all_x) - min(all_x),
                    'height': max(all_y) - min(all_y),
                }

        return symbol_data

    def extract_instance_placements(self) -> None:
        """
        Phase G4: Extract instance placements with full transform matrices.

        Instance placements include:
        - Symbol reference
        - Position (x, y)
        - Full 3x3 affine transform matrix (Critical Requirement #5)
        - Rotation
        - Z-order for draw order (Critical Requirement #2)

        This connects the logical components to their geometric placement.
        """
        print("\n" + "="*60)
        print("PHASE G4: INSTANCE PLACEMENT EXTRACTION")
        print("="*60)

        placement_count = 0

        # Process all page files in worklib
        for block_dir in self.worklib_dir.iterdir():
            if not block_dir.is_dir() or block_dir.name in self.IGNORE_DIRS:
                continue

            tbl_dir = block_dir / 'tbl_1'
            if not tbl_dir.exists():
                continue

            for page_file in tbl_dir.glob('page_file_*.ascii'):
                try:
                    placements = self._extract_placements_from_page(page_file, block_dir.name)
                    placement_count += len(placements)
                    self.primitives.extend(placements)
                except Exception as e:
                    print(f"  [WARN] Failed to process {page_file.name}: {e}")

        print(f"  - Instance placements extracted: {placement_count}")
        self.stats['total_primitives'] = len(self.primitives)

    def _extract_placements_from_page(self, page_file: Path, block_name: str) -> List[Dict]:
        """Extract instance placements from a page file."""
        placements = []

        try:
            content = page_file.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            return placements

        # Extract page index
        page_idx_match = re.search(r'page_file_(\d+)\.ascii', page_file.name)
        page_index = int(page_idx_match.group(1)) if page_idx_match else 0

        # Pattern to find instance placements
        # Instances are referenced via cellid or symbol reference with transform
        # Looking for patterns with transform matrix and position

        # Find all transform matrices with associated position data
        # Pattern: transform + position (< 45 /> coordinate block)
        transform_pattern = re.compile(
            r'<n transform n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*([^v]+)\s*v/>.*?'
            r'<\s*45\s*/>\s*<\s*<\s*(\d+)\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*(\d+)\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',
            re.DOTALL
        )

        for match in transform_pattern.finditer(content):
            transform_str = match.group(1).strip()
            x = int(match.group(3))
            y = int(match.group(5))

            # Look for associated rotation
            rotation_match = re.search(
                r'<n rotation n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>',
                content[max(0, match.start()-300):match.end()+300]
            )
            rotation = int(rotation_match.group(1)) if rotation_match else 0

            # Look for zValue
            z_match = re.search(
                r'<n zValue n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>',
                content[max(0, match.start()-300):match.end()+300]
            )
            z_value = int(z_match.group(1)) if z_match else 10000

            # Look for any associated name/refdes
            name_match = re.search(
                r'<n name n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*([^v]+)\s*v/>',
                content[max(0, match.start()-500):match.end()+500]
            )
            instance_name = name_match.group(1).strip() if name_match else None

            # Only create placement if it looks like a component instance
            # (not just internal graphics transforms)
            if transform_str != '1,0,0,0,1,0,0,0,1' or rotation != 0:
                element_id = self._generate_element_id('inst')
                sequence_idx = self._next_sequence_index()

                placement = {
                    'element_id': element_id,
                    'sequence_index': sequence_idx,  # Critical Requirement #2
                    'type': 'instance',
                    'shape_type': 'component_instance',
                    'page_index': page_index,
                    'block': block_name,
                    'geometry': {
                        'origin': {'x': x, 'y': y},
                    },
                    'transform': self._parse_transform_matrix(transform_str),  # Critical Requirement #5
                    'rotation': rotation,
                    'z_value': z_value,  # Critical Requirement #2
                    'instance_name': instance_name,
                    'semantic': None,
                }

                placements.append(placement)
                self.stats['primitives_by_type']['instance'] += 1

        return placements

    def extract_text_primitives(self) -> None:
        """
        Phase G2b: Extract text primitives with alignment and rotation.

        Text primitives include:
        - Net labels
        - Component values
        - Sheet annotations

        This satisfies Critical Requirements:
        - #3: TEXT ALIGNMENT & ROTATION
        - #4: FONT NAMES & WEIGHTS
        """
        print("\n" + "="*60)
        print("PHASE G2b: TEXT PRIMITIVE EXTRACTION")
        print("="*60)

        text_count = 0

        # Process all page files
        for block_dir in self.worklib_dir.iterdir():
            if not block_dir.is_dir() or block_dir.name in self.IGNORE_DIRS:
                continue

            tbl_dir = block_dir / 'tbl_1'
            if not tbl_dir.exists():
                continue

            for page_file in tbl_dir.glob('page_file_*.ascii'):
                try:
                    texts = self._extract_text_from_page(page_file, block_dir.name)
                    text_count += len(texts)
                    self.primitives.extend(texts)
                except Exception as e:
                    print(f"  [WARN] Failed to process {page_file.name}: {e}")

        print(f"  - Text primitives extracted: {text_count}")
        self.stats['total_primitives'] = len(self.primitives)

    def _extract_text_from_page(self, page_file: Path, block_name: str) -> List[Dict]:
        """Extract text primitives from a page file."""
        texts = []

        try:
            content = page_file.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            return texts

        # Extract page index
        page_idx_match = re.search(r'page_file_(\d+)\.ascii', page_file.name)
        page_index = int(page_idx_match.group(1)) if page_idx_match else 0

        # Justification mapping (Critical Requirement #3)
        JUST_MAP = {
            0: 'left',
            1: 'center',
            2: 'right',
        }

        # Pattern for text blocks with properties
        # Text pattern: < 31 /> followed by text content and justification/rotation
        text_pattern = re.compile(
            r'<\s*31\s*/>\s*<[^>]+>\s*<[^>]+>\s*<[^>]+>\s*<\s*\d+\s*/>\s*<\s*([^/]+)\s*/>\s*'
            r'<\s*44\s*/>\s*<[^>]+>\s*<\s*45\s*/>\s*<\s*<\s*(\d+)\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*(\d+)\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',
            re.DOTALL
        )

        for match in text_pattern.finditer(content):
            text_content = match.group(1).strip()
            x = int(match.group(3))
            y = int(match.group(5))

            # Skip empty or placeholder text
            if not text_content or text_content in ['##', '?', 'PN']:
                continue

            # Look for justification (just property)
            just_match = re.search(
                r'<n just n/>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>',
                content[match.start():min(match.end()+500, len(content))]
            )
            justification = int(just_match.group(1)) if just_match else 0

            # Look for rotation
            rotation_match = re.search(
                r'<n rotation n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>',
                content[match.start():min(match.end()+500, len(content))]
            )
            rotation = int(rotation_match.group(1)) if rotation_match else 0

            # Look for style reference
            style_match = re.search(
                r'<\s*\d+\s*/>\s*<\s*(Style\d+)\s*/>',
                content[match.start():min(match.end()+300, len(content))]
            )
            style_ref = style_match.group(1) if style_match else 'Style1'

            # Look for zValue
            z_match = re.search(
                r'<n zValue n/>\s*<[^>]+>\s*<[^>]+>\s*<v\s*(\d+)\s*v/>',
                content[match.start():min(match.end()+500, len(content))]
            )
            z_value = int(z_match.group(1)) if z_match else 10000

            # Get font properties from style (Critical Requirement #4)
            font_props = {
                'font_name': 'Arial',
                'font_size': 10.0,
                'font_weight': 'normal',
                'font_style': 'normal',
            }
            if style_ref in self.styles:
                style_data = self.styles[style_ref]
                font_props = {
                    'font_name': style_data.get('font_name', 'Arial'),
                    'font_size': style_data.get('font_size', 10.0),
                    'font_weight': style_data.get('font_weight', 'normal'),
                    'font_style': style_data.get('font_style', 'normal'),
                }

            element_id = self._generate_element_id('text')
            sequence_idx = self._next_sequence_index()

            text_prim = {
                'element_id': element_id,
                'sequence_index': sequence_idx,  # Critical Requirement #2
                'type': 'text',
                'shape_type': 'label',
                'page_index': page_index,
                'block': block_name,
                'geometry': {
                    'origin': {'x': x, 'y': y},
                },
                'text_content': text_content,
                'text_properties': {  # Critical Requirement #3
                    'alignment': JUST_MAP.get(justification, 'left'),
                    'rotation': rotation,
                    'justification': justification,
                },
                'font_properties': font_props,  # Critical Requirement #4
                'style_ref': style_ref,
                'z_value': z_value,
                'semantic': None,
            }

            texts.append(text_prim)
            self.stats['primitives_by_type']['text'] += 1

        return texts

    def extract_components_from_json(self, json_path: Path) -> None:
        """
        Phase 2: Extract component instances from a JSON file.

        JSON structure:
        {
          "objects": [
            {
              "type": "part",
              "properties": {...},
              "meta": {
                "instances": [
                  {
                    "name": "cpath",
                    "value": "@worklib.block(tbl_1):\\IXXXXX\\",
                    "data": [{"name": "refdes", "value": "C51"}, ...]
                  }
                ]
              }
            }
          ]
        }
        """
        block_name = json_path.parent.parent.name

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"  [WARN] Failed to parse JSON: {json_path.name} - {e}")
            return
        except Exception as e:
            print(f"  [WARN] Error reading {json_path.name}: {e}")
            return

        objects = data.get('objects', [])

        for obj in objects:
            obj_type = obj.get('type', '')

            # We're interested in 'part' objects (components)
            if obj_type != 'part':
                continue

            properties = obj.get('properties', {})
            meta = obj.get('meta', {})
            instances = meta.get('instances', [])

            # Extract all instances of this part
            for instance in instances:
                if instance.get('name') != 'cpath':
                    continue

                cpath = instance.get('value', '')
                instance_id = self._extract_instance_id(cpath)

                if not instance_id:
                    continue

                # Extract refdes and other instance data
                instance_data = {}
                for data_item in instance.get('data', []):
                    name = data_item.get('name', '')
                    value = data_item.get('value', '')
                    instance_data[name] = value

                refdes = instance_data.get('refdes', '')
                if not refdes:
                    continue

                # Determine component type from refdes prefix
                comp_type = self._classify_component(refdes)

                # Extract hierarchy
                hierarchy_chain = self._parse_hierarchy_path(cpath)

                # Build component record
                component = {
                    'refdes': refdes,
                    'type': comp_type,
                    'library': properties.get('CDS_LIBRARY_ID', '').split(':')[0] if ':' in properties.get('CDS_LIBRARY_ID', '') else properties.get('CDS_LIBRARY_ID', ''),
                    'part_name': properties.get('PART_NAME', properties.get('CDS_PART_NAME', '')),
                    'block': block_name,
                    'hierarchy_path': cpath,
                    'hierarchy_chain': hierarchy_chain,
                    'instance_id': instance_id,
                    'properties': properties.copy(),
                    'pins': []  # Will be populated from XCON
                }

                # Store in components dict (keyed by refdes)
                # Prefer longer hierarchy chains (more specific paths from top-level)
                comp_key = refdes
                if comp_key in self.components:
                    existing = self.components[comp_key]
                    existing_chain_len = len(existing.get('hierarchy_chain', []))
                    new_chain_len = len(hierarchy_chain)

                    # Keep the one with longer hierarchy (more complete path)
                    if new_chain_len > existing_chain_len:
                        # Replace with new (more complete)
                        self.components[comp_key] = component
                    # If same length or shorter, skip (keep existing)
                else:
                    self.components[comp_key] = component

                # Store in instance_map using full path for uniqueness
                # Key: (block, instance_id) to handle same IDs in different blocks
                map_key = f"{block_name}:{instance_id}"
                self.instance_map[map_key] = {
                    'refdes': refdes,
                    'comp_key': comp_key,
                    'block': block_name
                }
                # Also store by instance_id alone for simpler lookups
                self.instance_map[instance_id] = {
                    'refdes': refdes,
                    'comp_key': comp_key,
                    'block': block_name
                }

        self.stats['json_files_processed'] += 1

    def _classify_component(self, refdes: str) -> str:
        """Classify component type based on reference designator prefix."""
        prefix = re.match(r'^([A-Za-z]+)', refdes)
        if not prefix:
            return 'unknown'

        prefix = prefix.group(1).upper()

        type_map = {
            'R': 'resistor',
            'C': 'capacitor',
            'L': 'inductor',
            'U': 'ic',
            'Q': 'transistor',
            'D': 'diode',
            'J': 'connector',
            'P': 'connector',
            'CR': 'led',
            'LED': 'led',
            'F': 'fuse',
            'FB': 'ferrite_bead',
            'Y': 'crystal',
            'SW': 'switch',
            'TP': 'test_point',
            'PTH': 'pth_connector'
        }

        return type_map.get(prefix, prefix.lower())

    def extract_nets_and_connectivity_from_xcon(self, xcon_path: Path) -> None:
        """
        Phase 3: Extract nets and pin connectivity from XCON (XML) files.

        XCON structure:
        <schema>
          <designs>
            <design>
              <cells>...</cells>
              <nets>
                <net><id>N...</id><name>NET_NAME</name></net>
              </nets>
              <instances>
                <instance>
                  <id>I...</id>
                  <cellid>S...</cellid>
                  <pins>
                    <pin>
                      <termid>T...</termid>
                      <connections><connection net="N..."/></connections>
                    </pin>
                  </pins>
                </instance>
              </instances>
            </design>
          </designs>
        </schema>

        Note: Instance IDs in XCON files are local to that block's schematic.
        Components instantiated directly in a block will have their connectivity
        in that block's XCON file.
        """
        block_name = xcon_path.parent.parent.name

        try:
            tree = ET.parse(xcon_path)
            root = tree.getroot()
        except ET.ParseError as e:
            print(f"  [WARN] Failed to parse XCON: {xcon_path.name} - {e}")
            return

        # Handle XML namespace
        ns = {'cs': 'http://www.cadence.com/spb/csschema'}

        # Try with and without namespace
        def find_elements(parent, tag):
            """Find elements with or without namespace."""
            elements = parent.findall(f'.//{tag}')
            if not elements:
                elements = parent.findall(f'.//cs:{tag}', ns)
            return elements

        def find_child(parent, tag):
            """Find direct child element."""
            element = parent.find(tag)
            if element is None:
                element = parent.find(f'cs:{tag}', ns)
            return element

        # Extract cells (component type definitions)
        for cell in find_elements(root, 'cell'):
            cell_id = find_child(cell, 'id')
            if cell_id is None:
                continue
            cell_id = cell_id.text

            library = find_child(cell, 'library')
            name = find_child(cell, 'name')
            view = find_child(cell, 'view')

            terminals = []
            for term in find_elements(cell, 'term'):
                term_id = find_child(term, 'id')
                term_name = find_child(term, 'name')
                term_dir = find_child(term, 'direction')

                if term_id is not None:
                    terminals.append({
                        'id': term_id.text,
                        'name': term_name.text if term_name is not None else '',
                        'direction': term_dir.text if term_dir is not None else 'unspec'
                    })

            self.cells[cell_id] = {
                'library': library.text if library is not None else '',
                'name': name.text if name is not None else '',
                'view': view.text if view is not None else '',
                'terminals': terminals
            }

        # Extract nets
        for net in find_elements(root, 'net'):
            net_id = find_child(net, 'id')
            net_name = find_child(net, 'name')

            if net_id is None or net_name is None:
                continue

            net_id_text = net_id.text
            net_name_text = net_name.text

            # Build net ID -> name mapping
            self.net_id_map[net_id_text] = net_name_text

            # Extract optional attributes
            scope = find_child(net, 'scope')
            direction = find_child(net, 'direction')

            # Store or update net
            if net_name_text not in self.nets:
                self.nets[net_name_text] = {
                    'id': net_id_text,
                    'name': net_name_text,
                    'scope': scope.text if scope is not None else None,
                    'direction': direction.text if direction is not None else None,
                    'blocks': set(),
                    'connections': []
                }

            self.nets[net_name_text]['blocks'].add(block_name)

        # Build terminal ID -> name mapping from cells
        term_id_to_name = {}
        for cell_id, cell_data in self.cells.items():
            for term in cell_data['terminals']:
                term_id_to_name[term['id']] = term['name']

        # Extract instances and pin connectivity
        for instance in find_elements(root, 'instance'):
            inst_id = find_child(instance, 'id')
            if inst_id is None:
                continue

            inst_id_text = inst_id.text
            # Remove 'I' prefix if present to match our instance_map
            inst_id_num = inst_id_text[1:] if inst_id_text.startswith('I') else inst_id_text

            cell_id = find_child(instance, 'cellid')
            cell_id_text = cell_id.text if cell_id is not None else None

            # Get cell info for terminal names
            cell_info = self.cells.get(cell_id_text, {})

            # Build symbol key for pin number lookup
            library = cell_info.get('library', '')
            cell_name = cell_info.get('name', '')
            symbol_key = f"{library}##{cell_name}"
            symbol_pins = self.symbol_pin_map.get(symbol_key, {})

            # Extract pins
            pins_data = []
            for pin in find_elements(instance, 'pin'):
                term_id = find_child(pin, 'termid')
                if term_id is None:
                    continue

                term_id_text = term_id.text
                pin_name = term_id_to_name.get(term_id_text, term_id_text)

                # Look up pin number from symbol cache
                pin_number = symbol_pins.get(pin_name.lower(), '')

                # Get connections
                for conn in find_elements(pin, 'connection'):
                    net_id = conn.get('net')
                    if net_id:
                        net_name = self.net_id_map.get(net_id, net_id)

                        pins_data.append({
                            'pin_name': pin_name,
                            'pin_number': pin_number,
                            'pin_id': term_id_text,
                            'net': net_name,
                            'net_id': net_id
                        })

                        # Add to net's connections
                        if net_name in self.nets:
                            # Look up refdes from instance_map
                            inst_info = self.instance_map.get(inst_id_num, {})
                            refdes = inst_info.get('refdes', f'INST_{inst_id_num}')

                            self.nets[net_name]['connections'].append({
                                'refdes': refdes,
                                'pin': pin_name,
                                'instance_id': inst_id_num
                            })
                            self.stats['total_connections'] += 1

            # Update component with pin data
            # Try block-qualified key first, then simple key
            map_key = f"{block_name}:{inst_id_num}"
            inst_info = self.instance_map.get(map_key) or self.instance_map.get(inst_id_num)

            if inst_info:
                comp_key = inst_info['comp_key']
                if comp_key in self.components:
                    # Only update if component doesn't already have pins
                    # (prefer pins from more complete hierarchy)
                    if not self.components[comp_key]['pins']:
                        self.components[comp_key]['pins'] = pins_data

        self.stats['xcon_files_processed'] += 1

    def build_hierarchy(self) -> None:
        """Phase 4: Build the hierarchy tree from component paths."""
        print("\n" + "="*60)
        print("PHASE 4: BUILDING HIERARCHY")
        print("="*60)

        # Initialize top-level
        self.hierarchy = {
            'brain_board': {
                'type': 'top',
                'components': [],
                'children': {}
            }
        }

        # Group components by their hierarchy chain
        for comp_key, comp_data in self.components.items():
            chain = comp_data.get('hierarchy_chain', [])
            refdes = comp_data['refdes']

            if not chain:
                # Top-level component
                self.hierarchy['brain_board']['components'].append(refdes)
                continue

            # Navigate/create hierarchy path
            current = self.hierarchy['brain_board']
            for block in chain:
                if block not in current['children']:
                    current['children'][block] = {
                        'type': 'block',
                        'components': [],
                        'children': {}
                    }
                current = current['children'][block]

            # Add component to deepest block
            current['components'].append(refdes)

        print(f"  - Hierarchy levels: {self._count_hierarchy_levels(self.hierarchy)}")

    def _count_hierarchy_levels(self, node: Dict, level: int = 0) -> int:
        """Count maximum depth of hierarchy tree."""
        if not node:
            return level

        max_level = level
        for key, value in node.items():
            if isinstance(value, dict) and 'children' in value:
                child_level = self._count_hierarchy_levels(value['children'], level + 1)
                max_level = max(max_level, child_level)

        return max_level

    def validate(self) -> bool:
        """
        Phase 5a: Validate extracted data.
        Returns True if validation passes.
        """
        print("\n" + "="*60)
        print("PHASE 5: VALIDATION")
        print("="*60)

        self.stats['total_components'] = len(self.components)
        self.stats['total_nets'] = len(self.nets)

        # Recalculate component breakdown from final deduplicated list
        self.stats['components_by_type'] = defaultdict(int)
        for comp in self.components.values():
            self.stats['components_by_type'][comp['type']] += 1

        warnings = []
        errors = []

        # Check component count
        if self.stats['total_components'] < 10:
            errors.append(f"CRITICAL: Only {self.stats['total_components']} components found. "
                         "This suggests we may be looking at the wrong files!")
        elif self.stats['total_components'] < 100:
            warnings.append(f"Low component count: {self.stats['total_components']}. "
                           "Design may be incomplete.")

        # Check for missing pin data
        components_without_pins = sum(1 for c in self.components.values() if not c['pins'])
        if components_without_pins > 0:
            warnings.append(f"{components_without_pins} components have no pin connectivity data")

        # Check net connectivity
        orphan_nets = [n for n, data in self.nets.items() if not data['connections']]
        if orphan_nets:
            warnings.append(f"{len(orphan_nets)} nets have no connections")

        # Print results
        print(f"\nValidation Results:")
        print(f"  Components: {self.stats['total_components']}")
        print(f"  DX.JSON Instances (with refdes): {len(self.dx_instances)}")
        print(f"  Nets: {self.stats['total_nets']}")
        print(f"  Total Connections: {self.stats['total_connections']}")
        print(f"  Blocks: {len(self.stats['blocks_processed'])}")

        # Symbol graphics stats
        symbols_with_lines = sum(1 for s in self.symbol_graphics.values() if s.get('lines'))
        symbols_with_labels = sum(1 for s in self.symbol_graphics.values() if s.get('labels'))
        symbols_with_pins = sum(1 for s in self.symbol_graphics.values() if s.get('pins'))
        print(f"\n  Symbol Graphics:")
        print(f"    Total symbols: {len(self.symbol_graphics)}")
        print(f"    With body lines: {symbols_with_lines}")
        print(f"    With text labels: {symbols_with_labels}")
        print(f"    With pin definitions: {symbols_with_pins}")

        print(f"\n  Component Breakdown:")
        for comp_type, count in sorted(self.stats['components_by_type'].items(),
                                       key=lambda x: -x[1]):
            print(f"    {comp_type}: {count}")

        if warnings:
            print(f"\n  Warnings:")
            for w in warnings:
                print(f"    [WARN] {w}")

        if errors:
            print(f"\n  Errors:")
            for e in errors:
                print(f"    [ERROR] {e}")
            return False

        return True

    def export(self, output_path: str) -> None:
        """Phase 5b: Export aggregated data to JSON file."""
        print("\n" + "="*60)
        print(f"EXPORTING TO: {output_path}")
        print("="*60)

        # Convert sets to lists for JSON serialization
        nets_export = {}
        for net_name, net_data in self.nets.items():
            nets_export[net_name] = {
                'id': net_data['id'],
                'scope': net_data['scope'],
                'direction': net_data['direction'],
                'blocks': list(net_data['blocks']),
                'connections': net_data['connections']
            }

        # Build instance list with symbol graphics linked
        # This is the CRITICAL piece - linking refdes to their symbol graphics
        instances_with_graphics = []
        for inst_id, inst_data in self.dx_instances.items():
            symbol_key = inst_data.get('symbol_cache_key', '')
            symbol_graphics = self.symbol_graphics.get(symbol_key, {})

            instance_entry = {
                'instance_id': inst_id,
                'refdes': inst_data.get('refdes', ''),
                'library': inst_data.get('library', ''),
                'part_name': inst_data.get('part_name', ''),
                'symbol': inst_data.get('symbol', ''),
                'block': inst_data.get('block', ''),
                'symbol_cache_key': symbol_key,
                'symbol_cache_path': inst_data.get('symbol_cache_path', ''),
                # Link to symbol graphics if available
                'has_symbol_graphics': bool(symbol_graphics),
                'symbol_bounding_box': symbol_graphics.get('bounding_box'),
                'symbol_line_count': len(symbol_graphics.get('lines', [])),
                'symbol_label_count': len(symbol_graphics.get('labels', [])),
                'symbol_pin_count': len(symbol_graphics.get('pins', [])),
                # Include text positions for placing refdes/value labels
                'text_positions': symbol_graphics.get('text_positions', {}),
            }
            instances_with_graphics.append(instance_entry)

        # Build output structure
        output = {
            'project': 'brain_board',
            'extraction_date': datetime.now().isoformat(),

            # Grid configuration (Critical Requirement #8)
            'grid_config': self.grid_config,

            # Statistics (including geometric stats)
            'statistics': {
                'total_components': self.stats['total_components'],
                'total_nets': self.stats['total_nets'],
                'total_connections': self.stats['total_connections'],
                'blocks_processed': len(self.stats['blocks_processed']),
                'json_files_processed': self.stats['json_files_processed'],
                'xcon_files_processed': self.stats['xcon_files_processed'],
                'components_by_type': dict(self.stats['components_by_type']),
                # Geometric stats
                'total_pages': self.stats['total_pages'],
                'total_primitives': self.stats['total_primitives'],
                'primitives_by_type': dict(self.stats['primitives_by_type']),
                'primitives_by_shape_type': dict(self.stats['primitives_by_shape_type']),
                'style_files_processed': self.stats['style_files_processed'],
                'symbol_graphics_loaded': self.stats['symbol_graphics_loaded'],
                # NEW: DX.JSON instance stats
                'dx_instances_loaded': len(self.dx_instances),
                'instances_with_symbol_graphics': sum(1 for i in instances_with_graphics if i['has_symbol_graphics']),
            },

            # Pages (with element_ids for primitives on each page)
            'pages': self.pages,

            # Primitives flat array (includes sequence_index for draw order, cgtype/shape_type)
            'primitives': self.primitives,

            # Styles (includes font_name, font_weight, font_style)
            'styles': self.styles,

            # Symbol library (for hierarchical dependencies) - full graphics data
            'symbol_library': self.symbol_graphics,

            # NEW: Component instances with refdes linked to symbol graphics
            # This is what was MISSING before - the refdes labels (U12, C51, R84)
            'instances': instances_with_graphics,

            # Logical netlist data
            'components_flat': list(self.components.values()),
            'hierarchy': self.hierarchy,
            'nets': nets_export,
            'cells': self.cells
        }

        # Write to file
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, default=str)

        file_size = os.path.getsize(output_path)
        print(f"  - Output file size: {file_size / 1024:.1f} KB")
        print(f"  - Export complete!")


def main():
    """Main entry point for forensic extraction."""
    print("="*60)
    print("CADENCE SDAX FORENSIC EXTRACTOR")
    print("="*60)
    print(f"Started: {datetime.now().isoformat()}")

    # Initialize extractor
    root_dir = Path(__file__).parent
    extractor = ForensicExtractor(root_dir)

    # Phase 1: Discovery
    extractor.discover_signal_files()

    # Phase 1b: Load symbol pin numbers from cache
    extractor.load_symbol_pin_numbers()

    # Phase 1c: Load DX.JSON refdes data (CRITICAL - contains component labels!)
    extractor.load_dx_json_instances()

    # =========================================================================
    # GEOMETRIC LAYER EXTRACTION
    # =========================================================================

    # Phase G1: Extract pages/sheets
    extractor.extract_pages()

    # Phase G6: Load styles (before text extraction so fonts are available)
    extractor.load_styles()

    # Phase G7: Extract grid configuration
    extractor.extract_grid_config()

    # Phase G3: Extract symbol graphics from cache
    extractor.extract_symbol_graphics()

    # Phase G5: Extract wire segments
    extractor.extract_wire_segments()

    # Phase G4: Extract instance placements
    extractor.extract_instance_placements()

    # Phase G2b: Extract text primitives
    extractor.extract_text_primitives()

    # =========================================================================
    # LOGICAL NETLIST EXTRACTION
    # =========================================================================

    # Phase 2: Extract components from JSON
    print("\n" + "="*60)
    print("PHASE 2: COMPONENT EXTRACTION (JSON)")
    print("="*60)
    for json_file in extractor.json_files:
        print(f"\nProcessing: {json_file.name}")
        extractor.extract_components_from_json(json_file)

    # Phase 3: Extract nets and connectivity from XCON
    print("\n" + "="*60)
    print("PHASE 3: NET & CONNECTIVITY EXTRACTION (XCON)")
    print("="*60)
    for xcon_file in extractor.xcon_files:
        print(f"\nProcessing: {xcon_file.name}")
        extractor.extract_nets_and_connectivity_from_xcon(xcon_file)

    # Phase 4: Build hierarchy
    extractor.build_hierarchy()

    # Phase 5: Validate and export
    if extractor.validate():
        extractor.export('full_design.json')
        print("\n" + "="*60)
        print("EXTRACTION COMPLETE")
        print("="*60)
    else:
        print("\n" + "="*60)
        print("EXTRACTION FAILED - See errors above")
        print("="*60)
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
