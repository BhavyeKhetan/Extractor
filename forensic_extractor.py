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

        # DX.json instance data: refdes -> {instance_id, library, part_name, symbol}
        # Changed from instance_id key to refdes key to avoid collision!
        self.dx_instances: Dict[str, Dict] = {}

        # CRITICAL: Page mapping from TOC - maps (block, pageuid) -> pdf_page_number
        # This enables pages 9-21 which are distributed across multiple blocks
        self.page_mapping: Dict[Tuple[str, str], int] = {}

        # CRITICAL: Instance position chain mappings
        # Step 1: instance_id (I167231504) -> graphics_id (864692227966763070)
        self.instance_to_graphics: Dict[str, str] = {}
        # Step 2: graphics_id -> {x, y, page_file, block, page_index}
        self.graphics_positions: Dict[str, Dict] = {}
        # Step 3: Final merged: instance_id -> full position data
        self.instance_positions: Dict[str, Dict] = {}

        # Geometric layer data
        self.pages: List[Dict] = []  # List of page definitions
        self.primitives: List[Dict] = []  # Flat list of all primitives
        self.styles: Dict[str, Dict] = {}  # Style definitions from .style files
        self.symbol_graphics: Dict[str, Dict] = {}  # Symbol graphics from cache
        self.grid_config: Dict = {}  # Grid/snap configuration
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

        # Directories to ignore
        self.IGNORE_DIRS = {'test', 'cache', 'Configurations2', 'Thumbnails', 'META-INF', '.DS_Store', 'rendered_output'}

        # Block aliases for mapping filesystem names to TOC names
        self.BLOCK_ALIASES = {
            'hdmi_block_2': 'hdmi_block'
        }

        # Reverse mapping: TOC block name -> filesystem block name
        self.BLOCK_ALIASES_REVERSE = {v: k for k, v in self.BLOCK_ALIASES.items()}

    def discover_signal_files(self) -> None:
        """Phase 1: Discover JSON, DX.JSON, and XCON signal files in worklib."""
        print("\n" + "="*60)
        print("PHASE 1: SIGNAL FILE DISCOVERY")
        print("="*60)

        for block_dir in self.worklib_dir.iterdir():
            if not block_dir.is_dir():
                continue
            if block_dir.name in self.IGNORE_DIRS:
                continue

            block_name = block_dir.name
            tbl_dir = block_dir / 'tbl_1'

            if tbl_dir.exists():
                # Find JSON files (component definitions)
                for f in tbl_dir.glob('*.json'):
                    if self.DX_JSON_PATTERN.match(f.name):
                        self.dx_json_files.append(f)
                        print(f"  Found DX.JSON: {f.relative_to(self.root_dir)}")
                    elif self.JSON_PATTERN.match(f.name):
                        self.json_files.append(f)
                        print(f"  Found JSON: {f.relative_to(self.root_dir)}")

                # Find XCON files (connectivity)
                for f in tbl_dir.glob('*.xcon'):
                    self.xcon_files.append(f)
                    print(f"  Found XCON: {f.relative_to(self.root_dir)}")

        print(f"\n  Total JSON files: {len(self.json_files)}")
        print(f"  Total DX.JSON files: {len(self.dx_json_files)}")
        print(f"  Total XCON files: {len(self.xcon_files)}")

    def load_symbol_pin_numbers(self) -> None:
        """Load symbol pin numbers from cache."""
        print("\n" + "="*60)
        print("PHASE 1b: SYMBOL PIN NUMBERS")
        print("="*60)
        # Symbol pin info loaded during extract_symbol_graphics
        print("  (Loaded during symbol graphics extraction)")

    def load_dx_json_instances(self) -> None:
        """Load DX.JSON files containing refdes labels and symbol linking data."""
        print("\n" + "="*60)
        print("PHASE 1c: DX.JSON INSTANCE DATA")
        print("="*60)

        for dx_file in self.dx_json_files:
            try:
                with open(dx_file, 'r') as f:
                    data = json.load(f)

                instances = data.get('instances', [])
                loaded_count = 0
                for inst in instances:
                    # Data is in 'attributes' dict, not at top level
                    attributes = inst.get('attributes', {})
                    refdes = attributes.get('refdes', '')

                    if refdes:
                        library = attributes.get('library', '')
                        system_capture_model = attributes.get('system_capture_model', '')
                        cpath = inst.get('cpath', '')

                        # Extract instance_id from cpath
                        # Format: @worklib.usb_block(tbl_1):\I167231535\
                        instance_id = self._extract_instance_id_from_cpath(cpath)

                        # Build symbol_cache_key to link to symbol_library
                        # Format: library##system_capture_model (e.g., "discrete##capacitor")
                        symbol_cache_key = None
                        if library and system_capture_model:
                            symbol_cache_key = f"{library}##{system_capture_model}"

                        # Extract actual block name from cpath (not file location!)
                        # Hierarchical cpath: @worklib.brain_board(tbl_1):\I1039646744\@worklib.dsp_block(tbl_1):\I167231522\
                        # We want the LEAF block (last one in the chain) - dsp_block in this case
                        block_name = self._extract_block_from_cpath(cpath)
                        if not block_name:
                            block_name = dx_file.parent.parent.name

                        self.dx_instances[refdes] = {
                            'instance_id': instance_id,
                            'library': library,
                            'system_capture_model': system_capture_model,
                            'symbol': attributes.get('symbol', ''),
                            'symbol_cache_key': symbol_cache_key,
                            'block': block_name,
                            'cpath': cpath
                        }
                        loaded_count += 1

                print(f"  Loaded {loaded_count} instances from {dx_file.name}")
            except Exception as e:
                print(f"  Error loading {dx_file.name}: {e}")

        # Print linking statistics
        with_key = sum(1 for v in self.dx_instances.values() if v.get('symbol_cache_key'))
        print(f"\n  Total DX instances loaded: {len(self.dx_instances)}")
        print(f"  Instances with symbol_cache_key: {with_key}")

    def build_instance_to_graphics_mapping(self) -> None:
        """Build mapping from instance_id to graphics_id using block.ascii files."""
        print("\n" + "="*60)
        print("PHASE 1d: INSTANCE TO GRAPHICS MAPPING")
        print("="*60)

        for block_dir in self.worklib_dir.iterdir():
            if not block_dir.is_dir() or block_dir.name in self.IGNORE_DIRS:
                continue

            # Block ascii files are named like: usb_block.ascii (same name as block dir)
            block_ascii = block_dir / 'tbl_1' / f'{block_dir.name}.ascii'
            if not block_ascii.exists():
                continue

            try:
                content = block_ascii.read_text(errors='ignore')

                # Format in block.ascii: < 5 /> I167231504 1 864692227966763070
                # This is: < 5 /> instance_id page_num graphics_id (with spaces around angle brackets)
                # Page numbers can be 1-8 so use \d+ for the page number field
                for match in re.finditer(r'< 5 /> (I\d+) \d+ (\d+)', content):
                    inst_id = match.group(1)
                    graphics_id = match.group(2)
                    self.instance_to_graphics[inst_id] = graphics_id

            except Exception as e:
                print(f"  Error processing {block_ascii}: {e}")

        print(f"  Total instance->graphics mappings: {len(self.instance_to_graphics)}")

    def extract_graphics_positions_from_pages(self) -> None:
        """Extract graphics positions from page files."""
        print("\n" + "="*60)
        print("PHASE 1e: GRAPHICS POSITIONS FROM PAGES")
        print("="*60)

        for block_dir in self.worklib_dir.iterdir():
            if not block_dir.is_dir() or block_dir.name in self.IGNORE_DIRS:
                continue

            tbl_dir = block_dir / 'tbl_1'
            if not tbl_dir.exists():
                continue

            block_name = block_dir.name

            for page_file in tbl_dir.glob('page_file_*.ascii'):
                try:
                    content = page_file.read_text(errors='ignore')
                    # Pattern: < GRAPHICS_ID /> < 45 /> < < 0 /> < X /> < 0 /> < Y /> />
                    # Example: < 864692227966763070 /> < 45 /> < < 0 /> < 1079500 /> < 0 /> < 647700 /> />
                    # The 18-digit graphics_id is followed by coordinates in < 45 /> block
                    pattern = r'< (\d{18}) />\s*<\s*45\s*/>\s*<\s*<\s*0\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*0\s*/>\s*<\s*(-?\d+)\s*/>'
                    for match in re.finditer(pattern, content):
                        gid = match.group(1)
                        x = int(match.group(2))
                        y = int(match.group(3))

                        # Get page index
                        page_idx = self._get_pdf_page_index(block_name, page_file.name)

                        self.graphics_positions[gid] = {
                            'x': x,
                            'y': y,
                            'page_file': page_file.name,
                            'block': block_name,
                            'page_index': page_idx
                        }
                except Exception as e:
                    print(f"  Error processing {page_file}: {e}")

        print(f"  Total graphics positions: {len(self.graphics_positions)}")

    def link_instance_positions(self) -> None:
        """Link instance_id -> graphics_id -> position."""
        print("\n" + "="*60)
        print("PHASE 1f: LINKING INSTANCE POSITIONS")
        print("="*60)

        linked = 0
        for inst_id, graphics_id in self.instance_to_graphics.items():
            if graphics_id in self.graphics_positions:
                pos = self.graphics_positions[graphics_id]
                self.instance_positions[inst_id] = {
                    'graphics_id': graphics_id,
                    **pos
                }
                linked += 1

        print(f"  Linked {linked} instance positions")

    def _generate_element_id(self, prefix: str = 'elem') -> str:
        """Generate unique element ID for primitives."""
        self._element_counter += 1
        return f"{prefix}_{self._element_counter}"

    def _generate_sequence_id(self) -> int:
        """Generate unique sequence ID."""
        self._sequence_counter += 1
        return self._sequence_counter

    def _extract_instance_id(self, cpath: str) -> str:
        """Extract instance ID from component path."""
        # cpath format: "/brain_board/I167231504" or similar
        if '/' in cpath:
            parts = cpath.strip('/').split('/')
            if len(parts) >= 2:
                return parts[-1]
        return cpath

    def _extract_instance_id_from_cpath(self, cpath: str) -> str:
        """Extract instance ID from DX.json cpath format.

        DX.json cpath format: @worklib.usb_block(tbl_1):\\I167231535\\
        May have nested paths for hierarchical: @worklib.usb_block(tbl_1):\\I1039646780\\@worklib.reusable_usb_conn(tbl_1):\\I1039138834\\

        For hierarchical paths, return the last (leaf) instance ID.
        """
        if not cpath:
            return ''

        # Find all instance IDs (format: \I<digits>\)
        import re
        matches = re.findall(r'\\I(\d+)\\', cpath)
        if matches:
            # Return the last match (leaf instance for hierarchical)
            return f"I{matches[-1]}"

        # Fallback: try the original method
        return self._extract_instance_id(cpath)

    def _extract_block_from_cpath(self, cpath: str) -> str:
        """Extract block name from DX.json cpath format.

        DX.json cpath format: @worklib.usb_block(tbl_1):\\I167231535\\
        Hierarchical: @worklib.brain_board(tbl_1):\\I1039646744\\@worklib.dsp_block(tbl_1):\\I167231522\\

        For hierarchical paths, return the LEAF (last) block name - that's where the component lives.
        """
        if not cpath:
            return ''

        # Find all block names (format: @worklib.BLOCK_NAME(tbl_1))
        import re
        matches = re.findall(r'@worklib\.([a-zA-Z0-9_]+)\(tbl_1\)', cpath)
        if matches:
            # Return the last match (leaf block for hierarchical)
            return matches[-1]

        return ''

    def _next_sequence_index(self) -> int:
        """Get next sequence index for primitives."""
        self._sequence_counter += 1
        return self._sequence_counter

    def _parse_transform_matrix(self, transform_str: str) -> Dict:
        """Parse transform matrix string into components."""
        # Transform format: "a b c d tx ty" (6 values)
        # Represents: | a  b  0 |
        #             | c  d  0 |
        #             | tx ty 1 |
        result = {
            'a': 1.0, 'b': 0.0, 'c': 0.0, 'd': 1.0,
            'tx': 0.0, 'ty': 0.0,
            'rotation': 0.0, 'mirror': False
        }
        if not transform_str:
            return result

        try:
            parts = transform_str.strip().split()
            if len(parts) >= 6:
                result['a'] = float(parts[0])
                result['b'] = float(parts[1])
                result['c'] = float(parts[2])
                result['d'] = float(parts[3])
                result['tx'] = float(parts[4])
                result['ty'] = float(parts[5])

                # Calculate rotation from matrix
                import math
                a, b = result['a'], result['b']
                result['rotation'] = math.degrees(math.atan2(b, a))

                # Check for mirror (negative determinant)
                det = result['a'] * result['d'] - result['b'] * result['c']
                result['mirror'] = det < 0
        except (ValueError, IndexError):
            pass

        return result

    def _parse_hierarchy_path(self, cpath: str) -> List[str]:
        """Parse component path into hierarchy chain."""
        # cpath format: "/brain_board/I167231504"
        if not cpath:
            return []
        return [p for p in cpath.strip('/').split('/') if p]

    def extract_pages(self) -> None:
        """
        Phase G1: Extract page information and build page mapping.

        CRITICAL: This MUST run BEFORE extract_graphics_positions_from_pages()
        because it builds the page_mapping dictionary that _get_pdf_page_index() needs!

        Builds a mapping: (block_name, page_uid) -> pdf_page_number
        This allows primitives extracted from block page files to be correctly
        assigned to their PDF page numbers.
        """
        print("\n" + "="*60)
        print("PHASE G1: PAGE/SHEET EXTRACTION")
        print("="*60)

        # The page structure is already loaded from the hierarchy/TOC
        # We need to build the mapping based on block_ref and page_uid

        # Hardcoded page mapping based on brain_board design structure
        # This maps (block_name, page_file_name) -> pdf_page_number
        # Note: block_name is the FILESYSTEM name (e.g., hdmi_block_2), not TOC name

        page_definitions = [
            # PDF Page 1: TOC (brain_board, page 2)
            ('brain_board', 'page_file_2.ascii', 1),
            # PDF Page 2: Blocks (brain_board, page 1)
            ('brain_board', 'page_file_1.ascii', 2),
            # PDF Page 3: PHY - USB (usb_block, page 1)
            ('usb_block', 'page_file_1.ascii', 3),
            # PDF Page 4: PHY - HDMI (hdmi_block_2, page 1)
            ('hdmi_block_2', 'page_file_1.ascii', 4),
            # PDF Page 5: Reset Generation (mgmt_block, page 1)
            ('mgmt_block', 'page_file_1.ascii', 5),
            # PDF Page 6: Power Management (mgmt_block, page 2)
            ('mgmt_block', 'page_file_2.ascii', 6),
            # PDF Page 7: MT41K - DDR3 (ddr3_block, page 1)
            ('ddr3_block', 'page_file_1.ascii', 7),
            # PDF Pages 8-15: Zynq banks (zynq_block, by pageuid from TOC)
            # TOC pageuid mapping: 4->Bank 0, 3->Bank 500, 1->Bank 501, 2->Bank 502,
            #                      6->Bank 34, 7->Bank 35, 5->Bank 13, 8->Power/Ground
            ('zynq_block', 'page_file_4.ascii', 8),   # Bank 0 (pageuid=4)
            ('zynq_block', 'page_file_3.ascii', 9),   # Bank 500 (pageuid=3)
            ('zynq_block', 'page_file_1.ascii', 10),  # Bank 501 (pageuid=1)
            ('zynq_block', 'page_file_2.ascii', 11),  # Bank 502 (pageuid=2)
            ('zynq_block', 'page_file_6.ascii', 12),  # Bank 34 (pageuid=6)
            ('zynq_block', 'page_file_7.ascii', 13),  # Bank 35 (pageuid=7)
            ('zynq_block', 'page_file_5.ascii', 14),  # Bank 13 (pageuid=5)
            ('zynq_block', 'page_file_8.ascii', 15),  # Power/Ground (pageuid=8)
            # PDF Pages 16-19: DSP blocks (dsp_block, pages 1-4)
            ('dsp_block', 'page_file_1.ascii', 16),
            ('dsp_block', 'page_file_2.ascii', 17),
            ('dsp_block', 'page_file_3.ascii', 18),
            ('dsp_block', 'page_file_4.ascii', 19),
            # PDF Page 20: GigE PHY (gige_block, page 1)
            ('gige_block', 'page_file_1.ascii', 20),
        ]

        # Build the mapping
        for block, page_file, pdf_page in page_definitions:
            self.page_mapping[(block, page_file)] = pdf_page
            print(f"  Mapped: ({block}, {page_file}) -> PDF Page {pdf_page}")

        # Also handle reusable_usb_conn - it's a child of usb_block, shares page 3
        self.page_mapping[('reusable_usb_conn', 'page_file_1.ascii')] = 3
        print(f"  Mapped: (reusable_usb_conn, page_file_1.ascii) -> PDF Page 3")

        print(f"\n  Total page mappings: {len(self.page_mapping)}")

        # Also build the pages list for export
        self.pages = []
        page_titles = [
            (1, 'TOC', 'brain_board'),
            (2, 'Blocks', 'brain_board'),
            (3, 'PHY', 'usb_block'),
            (4, 'PHY', 'hdmi_block'),
            (5, 'Reset Generation', 'mgmt_block'),
            (6, 'Power Management', 'mgmt_block'),
            (7, 'MT41K', 'ddr3_block'),
            (8, 'Bank 0', 'zynq_block'),
            (9, 'Bank 500', 'zynq_block'),
            (10, 'Bank 501', 'zynq_block'),
            (11, 'Bank 502', 'zynq_block'),
            (12, 'Bank 34', 'zynq_block'),
            (13, 'Bank 35', 'zynq_block'),
            (14, 'Bank 13 and GPIO connector', 'zynq_block'),
            (15, 'Power and Ground', 'zynq_block'),
            (16, 'East eLink and Control', 'dsp_block'),
            (17, 'North and South eLink', 'dsp_block'),
            (18, 'West eLink and Power', 'dsp_block'),
            (19, 'Connectors', 'dsp_block'),
            (20, 'PHY', 'gige_block'),
        ]

        for page_num, title, block_ref in page_titles:
            self.pages.append({
                'page_id': str(page_num),
                'page_uid': str(page_num),
                'title': title,
                'block_path': f'/brain_board/{block_ref}({block_ref})',
                'block_ref': block_ref,
                'size': {'width': 17000, 'height': 11000, 'unit': 'mils'},
                'page_standard': 'ANSI',
                'coordinate_origin': 'bottom_left',
                'element_ids': [],
                'element_count': 0,
            })

        print(f"  Built {len(self.pages)} page definitions")

    def _get_pdf_page_index(self, block_name: str, page_file_name: str) -> int:
        """
        Get the PDF page number for a given block and page file.

        Args:
            block_name: Filesystem block name (e.g., 'hdmi_block_2')
            page_file_name: Page file name (e.g., 'page_file_1.ascii')

        Returns:
            PDF page number (1-indexed), or -1 if not found
        """
        # First try direct lookup
        key = (block_name, page_file_name)
        if key in self.page_mapping:
            return self.page_mapping[key]

        # Try with block alias (filesystem name -> TOC name)
        if block_name in self.BLOCK_ALIASES:
            aliased_block = self.BLOCK_ALIASES[block_name]
            key = (aliased_block, page_file_name)
            if key in self.page_mapping:
                return self.page_mapping[key]

        # Return -1 to indicate fallback needed
        return -1

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

        # Extract page index - use PDF page mapping for correct page number!
        page_index = self._get_pdf_page_index(block_name, page_file.name)
        if page_index == -1:
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
        linked_count = 0

        # APPROACH 1: Create placements from dx_instances + instance_positions chain
        # This provides proper refdes, symbol_cache_key, and position linkage
        for refdes, inst_data in self.dx_instances.items():
            instance_id = inst_data.get('instance_id', '')
            position = self.instance_positions.get(instance_id)

            if not position:
                continue

            # Get page index from position data
            block_name = inst_data.get('block', position.get('block', ''))
            page_file = position.get('page_file', '')
            page_index = self._get_pdf_page_index(block_name, page_file)

            if page_index == -1:
                # Fallback: extract from page_file name
                page_idx_match = re.search(r'page_file_(\d+)\.ascii', page_file)
                page_index = int(page_idx_match.group(1)) if page_idx_match else 0

            element_id = self._generate_element_id('inst')
            sequence_idx = self._next_sequence_index()

            placement = {
                'element_id': element_id,
                'sequence_index': sequence_idx,
                'type': 'instance',
                'shape_type': 'component_instance',
                'page_index': page_index,
                'block': block_name,
                'geometry': {
                    'origin': {'x': position['x'], 'y': position['y']},
                },
                'transform': {'matrix': [1, 0, 0, 0, 1, 0, 0, 0, 1]},  # Default identity
                'rotation': 0,
                'z_value': 10000,
                # CRITICAL: Link to symbol data!
                'refdes': refdes,
                'instance_name': refdes,
                'instance_id': instance_id,
                'symbol_cache_key': inst_data.get('symbol_cache_key'),
                'semantic': None,
            }

            self.primitives.append(placement)
            linked_count += 1
            self.stats['primitives_by_type']['instance'] += 1

        # APPROACH 2: Also process page files for additional transform data
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

        print(f"  - Instance placements from dx_instances: {linked_count}")
        print(f"  - Instance placements from page files: {placement_count}")
        print(f"  - Total instance placements: {linked_count + placement_count}")
        self.stats['total_primitives'] = len(self.primitives)

    def _extract_placements_from_page(self, page_file: Path, block_name: str) -> List[Dict]:
        """Extract instance placements from a page file."""
        placements = []

        try:
            content = page_file.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            return placements

        # Extract page index - use PDF page mapping for correct page number!
        page_index = self._get_pdf_page_index(block_name, page_file.name)
        if page_index == -1:
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

        # CRITICAL: Generate refdes/value labels from instance positions
        self._generate_instance_text_labels()

    def _generate_instance_text_labels(self) -> None:
        """
        CRITICAL: Generate refdes and value text labels from instance positions.

        This method creates text primitives for:
        - REFDES labels (U1, C51, R84) at the symbol's LOCATION position
        - VALUE labels (10uF, 1K) at the symbol's VALUE position

        The symbol cache contains text_positions with LOCATION and VALUE offsets.
        We apply these offsets to the instance position to get absolute coordinates.

        This was MISSING from the original extractor - zero refdes labels were emitted!
        """
        print("\n  Generating instance text labels (refdes/value)...")

        refdes_count = 0
        value_count = 0

        for refdes, inst_data in self.dx_instances.items():
            # Get instance position (keyed by instance_id, not refdes!)
            instance_id = inst_data.get('instance_id', '')
            position = self.instance_positions.get(instance_id)
            if not position:
                continue

            inst_x = position.get('x', 0)
            inst_y = position.get('y', 0)
            page_index = position.get('page_index', 0)
            block_name = inst_data.get('block', position.get('block', ''))

            # Get symbol graphics for text_positions
            symbol_cache_key = inst_data.get('symbol_cache_key', '')
            symbol_graphics = self.symbol_graphics.get(symbol_cache_key, {})
            text_positions = symbol_graphics.get('text_positions', {})

            # Generate LOCATION label (refdes)
            if 'LOCATION' in text_positions:
                loc = text_positions['LOCATION']
                loc_pos = loc.get('position', {})
                loc_x = loc_pos.get('x', 0)
                loc_y = loc_pos.get('y', 0)

                # Apply offset to instance position
                abs_x = inst_x + loc_x
                abs_y = inst_y + loc_y

                element_id = self._generate_element_id('refdes')
                sequence_idx = self._next_sequence_index()

                text_prim = {
                    'element_id': element_id,
                    'sequence_index': sequence_idx,
                    'type': 'text',
                    'shape_type': 'refdes_label',
                    'label_type': 'LOCATION',
                    'page_index': page_index,
                    'block': block_name,
                    'geometry': {
                        'origin': {'x': abs_x, 'y': abs_y},
                    },
                    'text_content': refdes,  # The actual refdes like "U12", "C51"
                    'text_properties': {
                        'alignment': 'left',
                        'rotation': loc.get('rotation', 0),
                        'justification': loc.get('justification', 0),
                    },
                    'style_ref': loc.get('style_ref', 'Style1'),
                    'z_value': 10000,
                    'semantic': {
                        'kind': 'refdes_label',
                        'refdes': refdes,
                        'instance_id': inst_data.get('instance_id', ''),
                    },
                }

                self.primitives.append(text_prim)
                refdes_count += 1
                self.stats['primitives_by_type']['text'] += 1

            # Generate VALUE label (component value)
            if 'VALUE' in text_positions:
                val = text_positions['VALUE']
                val_pos = val.get('position', {})
                val_x = val_pos.get('x', 0)
                val_y = val_pos.get('y', 0)

                # Get the actual value from properties
                properties = inst_data.get('properties', {})
                value_text = properties.get('VALUE', properties.get('value', '?'))

                # Skip if value is placeholder
                if value_text in ['?', '']:
                    # Use part name as fallback
                    value_text = inst_data.get('part_name', '?')

                # Apply offset to instance position
                abs_x = inst_x + val_x
                abs_y = inst_y + val_y

                element_id = self._generate_element_id('value')
                sequence_idx = self._next_sequence_index()

                text_prim = {
                    'element_id': element_id,
                    'sequence_index': sequence_idx,
                    'type': 'text',
                    'shape_type': 'value_label',
                    'label_type': 'VALUE',
                    'page_index': page_index,
                    'block': block_name,
                    'geometry': {
                        'origin': {'x': abs_x, 'y': abs_y},
                    },
                    'text_content': value_text,
                    'text_properties': {
                        'alignment': 'left',
                        'rotation': val.get('rotation', 0),
                        'justification': val.get('justification', 0),
                    },
                    'style_ref': val.get('style_ref', 'Style1'),
                    'z_value': 10000,
                    'semantic': {
                        'kind': 'value_label',
                        'refdes': refdes,
                        'instance_id': inst_data.get('instance_id', ''),
                    },
                }

                self.primitives.append(text_prim)
                value_count += 1
                self.stats['primitives_by_type']['text'] += 1

        print(f"  - Refdes labels generated: {refdes_count}")
        print(f"  - Value labels generated: {value_count}")
        self.stats['primitives_by_shape_type']['refdes_label'] = refdes_count
        self.stats['primitives_by_shape_type']['value_label'] = value_count

    def _extract_text_from_page(self, page_file: Path, block_name: str) -> List[Dict]:
        """
        Extract text primitives from a page file.

        Text in SDAX page files comes in several forms:
        1. Net labels in Tag 29 - signal names like P0_USB_DN, VCC, GND
        2. HTML text blocks - rich text in GRAPHICS_BLOCK_CHILD_TEXT
        3. Label placeholders (LOCATION, VALUE) - positions for refdes/value text

        The key is finding text that has:
        - A visible text value (not internal metadata)
        - A coordinate position (< 45 /> block)
        """
        texts = []

        try:
            content = page_file.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            return texts

        # Extract page index - use PDF page mapping for correct page number!
        page_index = self._get_pdf_page_index(block_name, page_file.name)
        if page_index == -1:
            page_idx_match = re.search(r'page_file_(\d+)\.ascii', page_file.name)
            page_index = int(page_idx_match.group(1)) if page_idx_match else 0

        # Justification mapping (Critical Requirement #3)
        JUST_MAP = {
            0: 'left',
            1: 'center',
            2: 'right',
            3: 'center',
        }

        # System/internal label types to skip
        SKIP_LABELS = {
            'CGTYPE', 'LP', 'MSB', 'LSB', 'PROP_WIDTH', 'COMMENT_BODY',
            'GRAPHICS_BLOCK_ID', 'GRAPHICS_BLOCK_NAME', 'BODY_TYPE',
            'HDL_PORT', 'HDL_POWER', 'NC_PORT', 'PATH', 'LOCATION', 'VALUE',
            'IMPLEMENTATION', 'IMPLEMENTATION_TYPE', 'PSPICETEMPLATE',
            'ORGNAME', 'ORGADDR1', 'ORGADDR2', 'ORGADDR3', 'REVCODE',
            'PAGE_NUMBER', 'PAGE_COUNT', 'PAGE_SIZE', 'PAGE_CREATE_DATE',
            'CAP_NAME', 'OFFPAGE', 'DOC', 'VHDL_PORT', 'VHDL_MODE',
            'CDS_NET_ID', 'HDL_TAP', 'VOLTAGE', 'MFG_PART_NO', 'MFG',
            'JEDEC_TYPE', 'DATASHEET', 'ASI_MODEL', 'ROHS',
            'CDS_LIBRARY_PHYSICAL_ID', 'CDS_LIBRARY_ID', 'CDS_ASSOC_NET_ID_STR',
            'zeronull', 'default', 'PN', 'BN', 'MPN',
        }

        seen_texts = set()  # Avoid duplicates

        # =====================================================================
        # PATTERN 1: Net name labels (P0_USB_DN, VCC, GND, etc.)
        # These appear as: < LENGTH /> < NET_NAME />
        # Position found in nearby < 45 /> blocks
        # =====================================================================

        # Pattern to find signal/net names
        net_name_pattern = re.compile(
            r'<\s*(\d+)\s*/>\s*<\s*'
            r'(P\d+_[A-Z0-9_]+|VCC[A-Z0-9_]*|GND[A-Z0-9_]*|PS_[A-Z0-9_]+|'
            r'CLK[A-Z0-9_]*|RST[A-Z0-9_]*|EN[A-Z0-9_]*|INT[A-Z0-9_]*|'
            r'SDA[A-Z0-9_]*|SCL[A-Z0-9_]*|MISO[A-Z0-9_]*|MOSI[A-Z0-9_]*|'
            r'TX[A-Z0-9_]*|RX[A-Z0-9_]*|[A-Z][A-Z0-9]*_[A-Z0-9_]+)'
            r'\s*/>'
        )

        for match in net_name_pattern.finditer(content):
            text_len = int(match.group(1))
            text = match.group(2).strip()

            # Skip if already seen
            if text in seen_texts:
                continue

            # Skip internal names
            if text in SKIP_LABELS:
                continue

            # Get context around this match to find position
            start = max(0, match.start() - 400)
            end = min(len(content), match.end() + 400)
            context = content[start:end]

            # Find ALL Tag 45 positions in context
            positions = re.findall(
                r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',
                context
            )

            # Filter for valid positions (not zeros from bounding boxes)
            valid_pos = [(int(x), int(y)) for x, y in positions
                        if abs(int(x)) > 1000 or abs(int(y)) > 1000]

            if not valid_pos:
                continue

            # Take position with largest magnitude (actual position, not offset)
            best_x, best_y = max(valid_pos, key=lambda p: abs(p[0]) + abs(p[1]))

            seen_texts.add(text)

            # Look for rotation/justification in context
            rot_match = re.search(r'<n\s+rotation\s+n/>\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<v\s*(-?\d+)\s*v/>', context)
            rotation = int(rot_match.group(1)) if rot_match else 0

            just_match = re.search(r'<n\s+just\s+n/>\s*<\s*\d+\s*/>\s*<v\s*(\d+)\s*v/>', context)
            justification = int(just_match.group(1)) if just_match else 0

            element_id = self._generate_element_id('netlabel')
            sequence_idx = self._next_sequence_index()

            text_prim = {
                'element_id': element_id,
                'sequence_index': sequence_idx,
                'type': 'text',
                'shape_type': 'net_label',
                'label_type': 'NET_NAME',
                'page_index': page_index,
                'block': block_name,
                'geometry': {
                    'origin': {'x': best_x, 'y': best_y},
                },
                'text_content': text,
                'text_properties': {
                    'alignment': JUST_MAP.get(justification, 'left'),
                    'rotation': rotation,
                    'justification': justification,
                },
                'style_ref': 'Style1',
                'z_value': 10000,
                'semantic': 'net_label',
            }

            texts.append(text_prim)
            self.stats['primitives_by_type']['text'] += 1

        # =====================================================================
        # PATTERN 2: HTML text blocks (GRAPHICS_BLOCK_CHILD_TEXT)
        # These contain rich text content in HTML format
        # =====================================================================

        html_text_pattern = re.compile(
            r'<n\s+GRAPHICS_BLOCK_CHILD_TEXT\s+n/>\s*<\s*\d+\s*/>\s*<\s*\d+\s*/>\s*<v\s*(.*?)\s*v/>',
            re.DOTALL
        )

        for match in html_text_pattern.finditer(content):
            html_content = match.group(1)

            # Extract plain text from HTML
            text_match = re.search(r'>([^<]+)</p>', html_content)
            if not text_match:
                continue

            text = text_match.group(1).strip()
            if not text or text in seen_texts:
                continue

            # Get context for position
            start = max(0, match.start() - 500)
            context = content[start:match.start()]

            # Find position in preceding context
            positions = re.findall(
                r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',
                context
            )

            if not positions:
                continue

            # Take last position before the text
            x, y = int(positions[-1][0]), int(positions[-1][1])

            seen_texts.add(text)

            element_id = self._generate_element_id('richtext')
            sequence_idx = self._next_sequence_index()

            text_prim = {
                'element_id': element_id,
                'sequence_index': sequence_idx,
                'type': 'text',
                'shape_type': 'annotation',
                'label_type': 'RICH_TEXT',
                'page_index': page_index,
                'block': block_name,
                'geometry': {
                    'origin': {'x': x, 'y': y},
                },
                'text_content': text,
                'text_properties': {
                    'alignment': 'center',
                    'rotation': 0,
                    'justification': 1,
                },
                'style_ref': 'Style1',
                'z_value': 10000,
                'semantic': 'annotation',
            }

            texts.append(text_prim)
            self.stats['primitives_by_type']['text'] += 1

        # =====================================================================
        # PATTERN 2b: Inline HTML text (embedded in Tag 29 blocks)
        # Example: <span style=" font-size:10pt; font-weight:600;">100 Ohm LVDS</span></p></body></html>
        # These are rich text annotations NOT wrapped by GRAPHICS_BLOCK_CHILD_TEXT
        # =====================================================================

        inline_html_pattern = re.compile(
            r'<span[^>]*>([^<]+)</span></p></body></html>\s*/>'
        )

        for match in inline_html_pattern.finditer(content):
            text = match.group(1).strip()

            # Skip if empty or already seen
            if not text or text in seen_texts:
                continue

            # Get larger context BEFORE the match to find the position
            start = max(0, match.start() - 1000)
            context = content[start:match.start()]

            # Find the LAST Tag 45 position before this text
            # Pattern: < 45 /> < < 0 /> < X /> < 0 /> < Y /> />
            positions = re.findall(
                r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',
                context
            )

            if not positions:
                continue

            # Take the LAST position (closest to the text)
            x, y = int(positions[-1][0]), int(positions[-1][1])

            # Skip positions that are zero (likely offsets, not actual positions)
            if abs(x) < 1000 and abs(y) < 1000:
                continue

            seen_texts.add(text)

            element_id = self._generate_element_id('htmltext')
            sequence_idx = self._next_sequence_index()

            text_prim = {
                'element_id': element_id,
                'sequence_index': sequence_idx,
                'type': 'text',
                'shape_type': 'annotation',
                'label_type': 'HTML_TEXT',
                'page_index': page_index,
                'block': block_name,
                'geometry': {
                    'origin': {'x': x, 'y': y},
                },
                'text_content': text,
                'text_properties': {
                    'alignment': 'center',
                    'rotation': 0,
                    'justification': 1,
                },
                'style': {
                    'font_size': 10,
                    'font_weight': 'bold',
                },
                'z_value': 10000,
                'semantic': 'annotation',
            }

            texts.append(text_prim)
            self.stats['primitives_by_type']['text'] += 1

        # =====================================================================
        # PATTERN 3: Pin numbers and simple labels (single chars/numbers)
        # Format: < 31 /> < < ... /> < LENGTH /> < TEXT />
        # =====================================================================

        # Find simple pin labels (1, 2, A, B, etc.)
        pin_label_pattern = re.compile(
            r'<\s*31\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(\d+)\s*/>\s*<\s*([A-Z0-9])\s*/>\s*'
            r'<\s*44\s*/>\s*<\s*<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>\s*'
            r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>\s*/>\s*'
            r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',
            re.DOTALL
        )

        for match in pin_label_pattern.finditer(content):
            text = match.group(3).strip()
            # Use the last Tag 45 position (actual position, not offset)
            x = int(match.group(8))
            y = int(match.group(9))

            pos_key = f"pin_{x}_{y}_{text}"
            if pos_key in seen_texts:
                continue
            seen_texts.add(pos_key)

            element_id = self._generate_element_id('pinlabel')
            sequence_idx = self._next_sequence_index()

            text_prim = {
                'element_id': element_id,
                'sequence_index': sequence_idx,
                'type': 'text',
                'shape_type': 'pin_label',
                'label_type': 'PIN',
                'page_index': page_index,
                'block': block_name,
                'geometry': {
                    'origin': {'x': x, 'y': y},
                },
                'text_content': text,
                'text_properties': {
                    'alignment': 'center',
                    'rotation': 0,
                    'justification': 1,
                },
                'style_ref': 'Style1',
                'z_value': 10000,
                'semantic': 'pin_label',
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

        # Build instance list with symbol graphics AND POSITIONS linked
        # This is the CRITICAL piece - linking refdes to their symbol graphics AND page positions
        instances_with_graphics = []
        instances_with_positions = 0
        instances_with_symbol_key = 0

        for refdes, inst_data in self.dx_instances.items():
            # Get the symbol_cache_key that links to symbol_library
            symbol_key = inst_data.get('symbol_cache_key', '')
            if symbol_key:
                instances_with_symbol_key += 1

            symbol_graphics = self.symbol_graphics.get(symbol_key, {})

            # CRITICAL: Get position data using actual instance_id (not refdes!)
            # instance_positions is keyed by instance_id (e.g., "I167231504")
            actual_instance_id = inst_data.get('instance_id', '')
            position_data = self.instance_positions.get(actual_instance_id, {})

            instance_entry = {
                'instance_id': actual_instance_id,
                'refdes': refdes,  # refdes is the key in dx_instances
                'library': inst_data.get('library', ''),
                'system_capture_model': inst_data.get('system_capture_model', ''),
                'symbol': inst_data.get('symbol', ''),
                'block': inst_data.get('block', ''),
                'symbol_cache_key': symbol_key,

                # CRITICAL: Position data (from page files via graphics_id chain)
                'has_position': bool(position_data),
                'x': position_data.get('x'),
                'y': position_data.get('y'),
                'page_index': position_data.get('page_index'),
                'page_file': position_data.get('page_file'),
                'graphics_id': position_data.get('graphics_id'),

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

            if position_data:
                instances_with_positions += 1

        print(f"  Instances with symbol_cache_key: {instances_with_symbol_key}")
        print(f"  Instances with positions: {instances_with_positions}")

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
                # CRITICAL: Position linking stats
                'instances_with_positions': instances_with_positions,
                'instance_to_graphics_mappings': len(self.instance_to_graphics),
                'graphics_positions_found': len(self.graphics_positions),
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

    # Phase 1d: Build instance_id -> graphics_id mapping from block.ascii files
    extractor.build_instance_to_graphics_mapping()

    # =========================================================================
    # CRITICAL: extract_pages() MUST run BEFORE extract_graphics_positions_from_pages()
    # because it builds the page_mapping dictionary that _get_pdf_page_index() needs!
    # =========================================================================

    # Phase G1: Extract pages/sheets - MUST RUN FIRST to build page_mapping!
    extractor.extract_pages()

    # Phase 1e: Extract graphics positions from page files (now uses page_mapping)
    extractor.extract_graphics_positions_from_pages()

    # Phase 1f: Link the full chain: refdes -> instance_id -> graphics_id -> position
    extractor.link_instance_positions()

    # =========================================================================
    # GEOMETRIC LAYER EXTRACTION (continued)
    # =========================================================================

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
