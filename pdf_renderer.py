#!/usr/bin/env python3
"""
ReportLab-based PDF Renderer for SDAX Schematic Data

Renders full_design.json to a pixel-perfect PDF matching the original
Cadence Allegro schematic output.

Usage:
    python pdf_renderer.py [input.json] [output.pdf]
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from reportlab.lib.pagesizes import landscape
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.colors import HexColor, black, white
import re
from pathlib import Path


# Allegro dark theme colors
DARK_BACKGROUND = '#1a1a2e'  # Dark navy background
IC_BODY_FILL = '#404040'     # Dark gray for IC bodies
IC_BODY_STROKE = '#808080'   # Light gray for IC outlines


class SchematicPDFRenderer:
    """Renders extracted schematic data to PDF using ReportLab."""

    # Page dimensions default to ANSI B unless overridden per page
    PAGE_WIDTH_INCHES = 17.0    # ANSI B width
    PAGE_HEIGHT_INCHES = 11.0   # ANSI B height

    # Page size in points (72 points per inch)
    PAGE_WIDTH = PAGE_WIDTH_INCHES * 72  # ~841 points
    PAGE_HEIGHT = PAGE_HEIGHT_INCHES * 72  # ~595 points

    # Internal units conversion: Cadence uses 254000 units per inch (~10,000 per mm)
    UNITS_PER_INCH = 254000
    SCALE = 72.0 / UNITS_PER_INCH

    def __init__(self, design_data: Dict):
        """Initialize renderer with extracted design data."""
        self.data = design_data
        self.pages = design_data.get('pages', [])
        self.primitives = design_data.get('primitives', [])
        self.instances = design_data.get('instances', [])
        self.symbol_library = design_data.get('symbol_library', {})
        self.styles = design_data.get('styles', {})
        self.nets = design_data.get('nets', {})

        # Create page index for quick lookup
        self.primitives_by_page = self._index_primitives_by_page()
        self.instances_by_page = self._index_instances_by_page()

        # Statistics
        self.stats = {
            'pages_rendered': 0,
            'wires_drawn': 0,
            'symbols_drawn': 0,
            'labels_drawn': 0,
        }

    def _index_primitives_by_page(self) -> Dict[int, List[Dict]]:
        """Index primitives by page number for efficient rendering."""
        by_page = {}
        for prim in self.primitives:
            page = prim.get('page_index')
            if page is not None:
                if page not in by_page:
                    by_page[page] = []
                by_page[page].append(prim)
        return by_page

    def _index_instances_by_page(self) -> Dict[int, List[Dict]]:
        """Index component instances by page number.

        Also builds a refdes->instance lookup for linking primitives to symbol data.
        """
        by_page = {}

        # Build refdes lookup for instances with symbol_cache_key
        self.instance_by_refdes = {}
        for inst in self.instances:
            refdes = inst.get('refdes')
            if refdes and inst.get('symbol_cache_key'):
                self.instance_by_refdes[refdes] = inst

        # Index by page - include instances that have positions
        for inst in self.instances:
            page = inst.get('page_index')
            if page is not None and inst.get('has_position', False):
                if page not in by_page:
                    by_page[page] = []
                by_page[page].append(inst)

        # DEBUG: Add all instances with symbol_cache_key to a test page (page 2)
        # This lets us verify symbol rendering works even without position data
        if 2 not in by_page:
            by_page[2] = []

        test_x = 500000  # Starting X position
        test_y = 3500000  # Starting Y position
        col = 0
        row = 0
        added = 0

        for inst in self.instances:
            if inst.get('symbol_cache_key') and not inst.get('has_position'):
                # Place in a grid on page 2 for testing
                test_inst = dict(inst)
                test_inst['x'] = test_x + (col * 300000)
                test_inst['y'] = test_y - (row * 200000)
                test_inst['has_position'] = True
                test_inst['page_index'] = 2
                by_page[2].append(test_inst)
                added += 1

                col += 1
                if col >= 5:  # 5 columns
                    col = 0
                    row += 1
                    if row >= 15:  # Max 15 rows = 75 symbols per page
                        break

        print(f"  DEBUG: Added {added} test instances to page 2")

        return by_page

    def to_pdf_coords(self, x: float, y: float) -> Tuple[float, float]:
        """Convert internal units to PDF coordinates.

        Internal coordinate system: origin at bottom-left, Y increases up
        ReportLab coordinate system: origin at bottom-left, Y increases up
        (Same orientation - no flip needed!)
        """
        pdf_x = x * self.SCALE
        pdf_y = y * self.SCALE
        return pdf_x, pdf_y

    def to_pdf_coords_fixed(self, x: float, y: float, page_num: int) -> Tuple[float, float]:
        """Convert titleblock/border coordinates to PDF coordinates.

        This uses FIXED page-size mapping (not content-based auto-fit).
        Titleblock coordinates are in internal units (0 to ~1,700,000 for ANSI B width).
        Maps directly to PDF page bounds.

        For top_left origin (Allegro): Y=0 is at top, Y increases downward
        For PDF: Y=0 is at bottom, Y increases upward
        So we flip: pdf_y = PAGE_HEIGHT - (y * scale)
        """
        pages = self.data.get('pages', [])
        page_info = pages[page_num - 1] if page_num - 1 < len(pages) else {}
        size = page_info.get('size', {'width': 17000, 'height': 11000, 'unit': 'mils'})

        # Page dimensions in internal units (mils * 100 = internal units)
        page_width_units = size.get('width', 17000) * 100  # 17000 mils = 1,700,000 units
        page_height_units = size.get('height', 11000) * 100  # 11000 mils = 1,100,000 units

        # Scale to fit PDF page
        scale_x = self.PAGE_WIDTH / page_width_units
        scale_y = self.PAGE_HEIGHT / page_height_units

        pdf_x = x * scale_x
        # Flip Y for top_left origin
        coord_origin = page_info.get('coordinate_origin', 'top_left')
        if coord_origin == 'top_left':
            pdf_y = self.PAGE_HEIGHT - (y * scale_y)
        else:
            pdf_y = y * scale_y

        return pdf_x, pdf_y

    def _calculate_page_bounds(self, page_num: int) -> Tuple[float, float, float, float]:
        """Calculate bounding box of content on a page."""
        prims = self.primitives_by_page.get(page_num, [])
        insts = self.instances_by_page.get(page_num, [])

        all_x, all_y = [], []

        for prim in prims:
            geo = prim.get('geometry', {})
            origin = geo.get('origin', {})
            if 'x' in origin:
                all_x.append(origin['x'])
            if 'y' in origin:
                all_y.append(origin['y'])
            for pt in geo.get('points', []):
                if 'x' in pt:
                    all_x.append(pt['x'])
                if 'y' in pt:
                    all_y.append(pt['y'])

        for inst in insts:
            if 'x' in inst:
                all_x.append(inst['x'])
            if 'y' in inst:
                all_y.append(inst['y'])

        if not all_x or not all_y:
            return 0, 0, 1700000, 1100000  # Default page bounds

        return min(all_x), min(all_y), max(all_x), max(all_y)

    def _get_page_transform(self, page_num: int) -> Tuple[float, float, float]:
        """Get scale and offset to fit content on page.

        Returns: (scale, offset_x, offset_y)
        """
        # Use cached transform if available
        if not hasattr(self, '_page_transforms'):
            self._page_transforms = {}

        if page_num in self._page_transforms:
            return self._page_transforms[page_num]

        # Use fixed page size from metadata instead of auto-fitting to content
        pages = self.data.get('pages', [])
        page_info = pages[page_num - 1] if page_num - 1 < len(pages) else {}
        size = page_info.get('size', {'width': 17000, 'height': 11000, 'unit': 'mils'})
        width_mils = size.get('width', 17000)
        height_mils = size.get('height', 11000)
        # Convert mils to internal units: mils -> inches (/1000) -> units
        data_width_units = (width_mils / 1000.0) * self.UNITS_PER_INCH
        data_height_units = (height_mils / 1000.0) * self.UNITS_PER_INCH

        # Scale to fit the page exactly
        usable_width = self.PAGE_WIDTH
        usable_height = self.PAGE_HEIGHT
        scale_x = usable_width / data_width_units
        scale_y = usable_height / data_height_units
        scale = min(scale_x, scale_y)

        # No extra margins; origin stays at 0,0 in page coords (top-left handled elsewhere)
        offset_x = 0
        offset_y = 0

        result = (scale, offset_x, offset_y)
        self._page_transforms[page_num] = result

        # Debug: print transform for each page
        print(f"    Page {page_num} fixed-fit: page_size_mils=({width_mils} x {height_mils}), "
              f"units=({data_width_units:.0f} x {data_height_units:.0f}), scale={scale:.6f}")

        return result

    def to_pdf_coords_page(self, x: float, y: float, page_num: int) -> Tuple[float, float]:
        """Transform coordinates with page-specific scale and offset.

        Y-axis handling is DATA-DRIVEN based on coordinate_origin:
        - 'bottom_left': No flip needed (Allegro and PDF both use Y-up from bottom)
        - 'top_left': Flip Y (would need pdf_y = PAGE_HEIGHT - pdf_y)
        """
        scale, offset_x, offset_y = self._get_page_transform(page_num)
        pdf_x = (x + offset_x) * scale
        pdf_y = (y + offset_y) * scale

        # Check page's coordinate_origin - only flip if origin is top_left
        pages = self.data.get('pages', [])
        if page_num < len(pages):
            coord_origin = pages[page_num].get('coordinate_origin', 'bottom_left')
            if coord_origin == 'top_left':
                pdf_y = self.PAGE_HEIGHT - pdf_y
            # 'bottom_left' = no flip (both Allegro and PDF use bottom-left origin with Y-up)

        return pdf_x, pdf_y

    def parse_color(self, color_str: str):
        """Parse hex color string to ReportLab color."""
        if not color_str or not color_str.startswith('#'):
            return black
        try:
            return HexColor(color_str)
        except:
            return black

    def render_to_pdf(self, output_path: str) -> None:
        """Render the complete schematic to PDF."""
        print(f"\n{'='*60}")
        print("REPORTLAB PDF RENDERER")
        print(f"{'='*60}")
        print(f"Output: {output_path}")
        print(f"Page size: {self.PAGE_WIDTH_INCHES}\" x {self.PAGE_HEIGHT_INCHES}\" (ANSI B)")
        print(f"Scale factor: {self.SCALE}")

        # Create PDF canvas
        c = canvas.Canvas(output_path, pagesize=(self.PAGE_WIDTH, self.PAGE_HEIGHT))

        # Get number of pages
        num_pages = len(self.pages) if self.pages else 20
        print(f"Pages to render: {num_pages}")

        for page_num in range(1, num_pages + 1):
            print(f"\n  Rendering page {page_num}...")
            self._render_page(c, page_num)
            self.stats['pages_rendered'] += 1

        # Save PDF
        c.save()

        print(f"\n{'='*60}")
        print("RENDERING COMPLETE")
        print(f"{'='*60}")
        print(f"  Pages rendered: {self.stats['pages_rendered']}")
        print(f"  Wires drawn: {self.stats['wires_drawn']}")
        print(f"  Symbols drawn: {self.stats['symbols_drawn']}")
        print(f"  Labels drawn: {self.stats['labels_drawn']}")
        print(f"  Output: {output_path}")

    def _render_page(self, c: canvas.Canvas, page_num: int) -> None:
        """Render a single page."""
        # Get page title if available
        page_info = self.pages[page_num - 1] if page_num <= len(self.pages) else {}
        page_title = page_info.get('title', f'Page {page_num}')

        # 0. Draw dark background first (matching Allegro theme)
        c.setFillColor(HexColor(DARK_BACKGROUND))
        c.rect(0, 0, self.PAGE_WIDTH, self.PAGE_HEIGHT, stroke=0, fill=1)

        # Render layers in order (back to front)
        # 0. Titleblock / border if available
        self._render_titleblock(c, page_num)
        # 1. Wire segments
        wires_on_page = self._render_wires(c, page_num)

        # 2. Component symbols
        symbols_on_page = self._render_symbols(c, page_num)

        # 3. Text labels (refdes, values, net names)
        labels_on_page = self._render_labels(c, page_num)

        print(f"    Page {page_num} ({page_title}): {wires_on_page} wires, {symbols_on_page} symbols, {labels_on_page} labels")

        # Add page number at bottom (white text on dark background)
        c.setFont("Helvetica", 8)
        c.setFillColor(white)
        c.drawString(self.PAGE_WIDTH / 2 - 20, 20, f"Page {page_num}")

        # Advance to next page
        c.showPage()

    def _render_wires(self, c: canvas.Canvas, page_num: int) -> int:
        """Render wire segments for a page."""
        count = 0

        # Get primitives for this page
        primitives = self.primitives_by_page.get(page_num, [])

        for prim in primitives:
            if prim.get('type') != 'line':
                continue
            if prim.get('shape_type') != 'wire':
                continue

            geometry = prim.get('geometry', {})
            points = geometry.get('points', [])

            if len(points) < 2:
                continue

            # Get style
            style = prim.get('style', {})
            line_width = style.get('line_width', 1) * 0.5  # Scale line width

            # Use cyan color for wires on dark background (since all extracted wires are black)
            extracted_color = style.get('line_color', '#000000')
            if extracted_color == '#000000':
                line_color = HexColor('#00ffff')  # Cyan for visibility on dark background
            else:
                line_color = self.parse_color(extracted_color)

            # Set drawing style
            c.setStrokeColor(line_color)
            c.setLineWidth(line_width)
            c.setLineCap(1)  # Round cap
            c.setLineJoin(1)  # Round join

            # Draw wire segment (use page-specific transform)
            x1, y1 = self.to_pdf_coords_page(points[0]['x'], points[0]['y'], page_num)
            x2, y2 = self.to_pdf_coords_page(points[1]['x'], points[1]['y'], page_num)

            c.line(x1, y1, x2, y2)

            count += 1
            self.stats['wires_drawn'] += 1

        return count

    def _get_style(self, style_ref: str) -> Dict:
        """Get style by reference, with fallback to default."""
        if style_ref and style_ref in self.styles:
            return self.styles[style_ref]
        return {
            'line_width': 1,
            'line_color': '#000000',
            'font_size': 8,
            'font_color': '#000000'
        }

    def _render_titleblock(self, c: canvas.Canvas, page_num: int) -> None:
        """Render ANSI titleblock/border based on page metadata.

        Uses FIXED page coordinate transform (not content-based auto-fit).
        """
        pages = self.data.get('pages', [])
        page_info = pages[page_num - 1] if page_num - 1 < len(pages) else {}
        # Determine which titleblock to use
        standard = page_info.get('pageBorderStandard', 'ANSI')
        size = page_info.get('pageBorderSize', 'B')

        # Map (standard, size) to symbol key in cache
        tb_map = {
            ('ANSI', 'B'): 'orcadlib##titleblockansilarge',
            ('ANSI', 'A'): 'orcadlib##titleblockansismall',
        }
        symbol_key = tb_map.get((standard, size))
        if not symbol_key:
            return

        symbol = self.symbol_library.get(symbol_key, {})
        if not symbol:
            symbol = self._parse_titleblock_from_cache(symbol_key)
        if not symbol:
            return

        # Draw lines using FIXED coordinate transform
        for line in symbol.get('lines', []):
            pts = line.get('points', [])
            if len(pts) < 2:
                continue
            style_ref = line.get('style_ref', '')
            style = self._get_style(style_ref)
            line_width = style.get('line_width', 1) * 0.5
            line_color = self.parse_color(style.get('line_color', '#FFFFFF'))
            c.setStrokeColor(line_color)
            c.setLineWidth(line_width)
            x1, y1 = self.to_pdf_coords_fixed(pts[0]['x'], pts[0]['y'], page_num)
            x2, y2 = self.to_pdf_coords_fixed(pts[1]['x'], pts[1]['y'], page_num)
            c.line(x1, y1, x2, y2)

        # Draw text labels using FIXED coordinate transform
        for label in symbol.get('labels', []):
            pos = label.get('position', {})
            txt = label.get('default_value', '') or label.get('name', '')
            style_ref = label.get('style_ref', '')
            style = self._get_style(style_ref)
            font_size = style.get('font_size', 8)
            font_color = self.parse_color(style.get('font_color', '#FFFFFF'))
            rotation = label.get('text_properties', {}).get('rotation', 0)

            c.setFont("Helvetica", font_size)
            c.setFillColor(font_color)
            pdf_x, pdf_y = self.to_pdf_coords_fixed(pos.get('x', 0), pos.get('y', 0), page_num)
            if rotation:
                c.saveState()
                c.translate(pdf_x, pdf_y)
                c.rotate(rotation)
                c.drawString(0, 0, txt)
                c.restoreState()
            else:
                c.drawString(pdf_x, pdf_y, txt)

        # Draw zones/grid if metadata present
        self._render_zones(c, page_num)

    def _render_zones(self, c: canvas.Canvas, page_num: int) -> None:
        """Render zone grid and labels using pageBorderZones metadata.

        Uses FIXED page coordinate transform (not content-based auto-fit).
        Zone format: "true#8#4#TBLLRN" = enabled, 8 columns, 4 rows, label positions
        """
        pages = self.data.get('pages', [])
        page_info = pages[page_num - 1] if page_num - 1 < len(pages) else {}
        zones = page_info.get('pageBorderZones', '')
        if not zones or not isinstance(zones, str):
            return
        try:
            parts = zones.split('#')
            enabled = parts[0].lower() == 'true'
            cols = int(parts[1])
            rows = int(parts[2])
        except Exception:
            return
        if not enabled or cols <= 0 or rows <= 0:
            return

        # Page size in internal units (mils * 100)
        size = page_info.get('size', {'width': 17000, 'height': 11000, 'unit': 'mils'})
        width_units = size.get('width', 17000) * 100  # e.g., 1,700,000
        height_units = size.get('height', 11000) * 100  # e.g., 1,100,000

        col_step = width_units / cols
        row_step = height_units / rows

        # Draw outer border using FIXED coordinates
        c.setStrokeColor(self.parse_color('#FFFFFF'))
        c.setLineWidth(1.5)

        # Border rectangle - draw at page edges
        # Use small margin for border inset
        margin = 20000  # ~0.08 inches inset
        x0, y0 = self.to_pdf_coords_fixed(margin, margin, page_num)
        x1, y1 = self.to_pdf_coords_fixed(width_units - margin, height_units - margin, page_num)
        c.rect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), stroke=1, fill=0)

        # Zone tick marks and labels
        c.setLineWidth(0.5)
        c.setFont("Helvetica", 10)
        c.setFillColor(self.parse_color('#FFFFFF'))

        tick_len = 15000  # Tick mark length in internal units

        # Column labels (1-8) at top and bottom
        for i in range(cols):
            x = margin + (i + 0.5) * col_step  # Center of zone
            label = str(i + 1)

            # Top tick and label
            tx, ty = self.to_pdf_coords_fixed(x, margin, page_num)
            c.drawCentredString(tx, ty + 5, label)

            # Bottom tick and label
            bx, by = self.to_pdf_coords_fixed(x, height_units - margin, page_num)
            c.drawCentredString(bx, by - 12, label)

            # Vertical tick marks at zone boundaries (except at edges)
            if i > 0:
                tick_x = margin + i * col_step
                # Top tick
                t1x, t1y = self.to_pdf_coords_fixed(tick_x, margin, page_num)
                t2x, t2y = self.to_pdf_coords_fixed(tick_x, margin + tick_len, page_num)
                c.line(t1x, t1y, t2x, t2y)
                # Bottom tick
                b1x, b1y = self.to_pdf_coords_fixed(tick_x, height_units - margin, page_num)
                b2x, b2y = self.to_pdf_coords_fixed(tick_x, height_units - margin - tick_len, page_num)
                c.line(b1x, b1y, b2x, b2y)

        # Row labels (A-D) at left and right
        for j in range(rows):
            y = margin + (j + 0.5) * row_step  # Center of zone
            label = chr(ord('A') + j)

            # Left label
            lx, ly = self.to_pdf_coords_fixed(margin, y, page_num)
            c.drawString(lx - 12, ly - 4, label)

            # Right label
            rx, ry = self.to_pdf_coords_fixed(width_units - margin, y, page_num)
            c.drawString(rx + 4, ry - 4, label)

            # Horizontal tick marks at zone boundaries (except at edges)
            if j > 0:
                tick_y = margin + j * row_step
                # Left tick
                l1x, l1y = self.to_pdf_coords_fixed(margin, tick_y, page_num)
                l2x, l2y = self.to_pdf_coords_fixed(margin + tick_len, tick_y, page_num)
                c.line(l1x, l1y, l2x, l2y)
                # Right tick
                r1x, r1y = self.to_pdf_coords_fixed(width_units - margin, tick_y, page_num)
                r2x, r2y = self.to_pdf_coords_fixed(width_units - margin - tick_len, tick_y, page_num)
                c.line(r1x, r1y, r2x, r2y)

    def _parse_titleblock_from_cache(self, symbol_key: str) -> Dict:
        """Parse a titleblock symbol directly from cache if not in symbol_library."""
        try:
            path = Path("cache") / f"{symbol_key}##sym_1.ascii"
            content = path.read_text(errors='ignore')
        except Exception:
            return {}

        symbol = {'lines': [], 'labels': []}

        # Lines (Tag 25) pattern
        line_pattern = re.compile(
            r'<\s*25\s*/>.*?'
            r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>\s*'
            r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',
            re.DOTALL
        )
        for m in line_pattern.finditer(content):
            x1, y1, x2, y2 = map(int, m.groups())
            symbol['lines'].append({'points': [{'x': x1, 'y': y1}, {'x': x2, 'y': y2}], 'style_ref': 'Style1'})

        # Text (Tag 29) pattern: label name then position
        text_pattern = re.compile(
            r'<\s*29\s*/>.*?<\s*([A-Za-z0-9_ :]+)\s*/>.*?'
            r'<\s*45\s*/>\s*<\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*<\s*\d+\s*/>\s*<\s*(-?\d+)\s*/>\s*/>',
            re.DOTALL
        )
        for m in text_pattern.finditer(content):
            name = m.group(1).strip()
            x = int(m.group(2))
            y = int(m.group(3))
            symbol['labels'].append({
                'name': name,
                'default_value': name,
                'position': {'x': x, 'y': y},
                'style_ref': 'Style1',
                'text_properties': {'rotation': 0}
            })

        return symbol

    def _render_symbols(self, c: canvas.Canvas, page_num: int) -> int:
        """Render component symbols for a page."""
        count = 0

        # Try instances from instances array first (has symbol_cache_key + positions)
        instances = self.instances_by_page.get(page_num, [])

        # If no positioned instances, try primitives with type='instance'
        # and link them to symbol data via instance_by_refdes
        if not instances:
            primitives = self.primitives_by_page.get(page_num, [])
            instance_prims = [p for p in primitives if p.get('type') == 'instance']

            # Try to enhance primitives with symbol data from dx_instances
            instances = []
            for prim in instance_prims:
                # Try to find matching instance by refdes or instance_name
                refdes = prim.get('refdes') or prim.get('instance_name')
                if refdes and hasattr(self, 'instance_by_refdes') and refdes in self.instance_by_refdes:
                    inst_data = self.instance_by_refdes[refdes]
                    # Merge primitive's geometry with instance's symbol data
                    merged = {**prim, **inst_data}
                    # Keep primitive's geometry for position
                    merged['geometry'] = prim.get('geometry', {})
                    instances.append(merged)
                else:
                    instances.append(prim)

        for inst in instances:
            # Get symbol graphics - check multiple key locations
            symbol_key = inst.get('symbol_cache_key', '')
            if not symbol_key:
                symbol_key = inst.get('symbol', '')
            symbol = self.symbol_library.get(symbol_key, {})

            # If no symbol found but we have a position, draw a placeholder box
            if not symbol:
                geometry = inst.get('geometry', {})
                origin = geometry.get('origin', {})
                inst_x = origin.get('x', 0)
                inst_y = origin.get('y', 0)

                # Only draw placeholder if we have a real position
                if inst_x != 0 or inst_y != 0:
                    pdf_x, pdf_y = self.to_pdf_coords_page(inst_x, inst_y, page_num)
                    # Draw a small magenta box as placeholder for unlinked symbols
                    c.setStrokeColor(HexColor('#FF00FF'))
                    c.setFillColor(HexColor('#FF00FF'))
                    c.setLineWidth(0.5)
                    c.rect(pdf_x - 5, pdf_y - 5, 10, 10, stroke=1, fill=0)
                    count += 1
                continue

            # Get instance position - handle both formats
            if 'x' in inst:
                inst_x = inst.get('x', 0)
                inst_y = inst.get('y', 0)
            else:
                # Primitives use geometry.origin
                geometry = inst.get('geometry', {})
                origin = geometry.get('origin', {})
                inst_x = origin.get('x', 0)
                inst_y = origin.get('y', 0)

            # Check if this is an IC (large component with many pins) - draw bounding box
            bounding_box = symbol.get('bounding_box', {})
            pins = symbol.get('pins', [])

            # IC symbols typically have many pins and large bounding boxes
            is_ic = len(pins) > 10 or (bounding_box.get('width', 0) > 200000 and bounding_box.get('height', 0) > 200000)

            if is_ic and bounding_box:
                # Draw IC body as filled rectangle (matching Allegro dark theme)
                min_x = bounding_box.get('min_x', 0)
                min_y = bounding_box.get('min_y', 0)
                max_x = bounding_box.get('max_x', 0)
                max_y = bounding_box.get('max_y', 0)

                # Transform to page coordinates
                pdf_x1, pdf_y1 = self.to_pdf_coords_page(inst_x + min_x, inst_y + min_y, page_num)
                pdf_x2, pdf_y2 = self.to_pdf_coords_page(inst_x + max_x, inst_y + max_y, page_num)

                # Draw IC body rectangle WITH FILL (dark gray body, light gray outline)
                c.setFillColor(HexColor(IC_BODY_FILL))
                c.setStrokeColor(HexColor(IC_BODY_STROKE))
                c.setLineWidth(1.0)
                c.rect(pdf_x1, pdf_y1, pdf_x2 - pdf_x1, pdf_y2 - pdf_y1, stroke=1, fill=1)

            # Draw symbol lines (FIXED: was 'body_lines', now 'lines')
            symbol_lines = symbol.get('lines', [])
            for line in symbol_lines:
                points = line.get('points', [])
                if len(points) < 2:
                    continue

                # Get style for this line
                style_ref = line.get('style_ref', '')
                style = self._get_style(style_ref)

                line_width = style.get('line_width', 1) * 0.5
                # Color remapping for visibility on dark background
                extracted_color = style.get('line_color', '#000000')
                if extracted_color.lower() in ['#000000', 'black', '#000']:
                    line_color = HexColor('#CCCCCC')  # Light gray for visibility
                elif extracted_color.lower() in ['#008000', 'green']:
                    line_color = HexColor('#00FF00')  # Bright lime green
                else:
                    line_color = self.parse_color(extracted_color)

                c.setStrokeColor(line_color)
                c.setLineWidth(line_width)

                # Transform first point to page coordinates
                px1 = inst_x + points[0].get('x', 0)
                py1 = inst_y + points[0].get('y', 0)
                pdf_x1, pdf_y1 = self.to_pdf_coords_page(px1, py1, page_num)

                # Create path
                path = c.beginPath()
                path.moveTo(pdf_x1, pdf_y1)

                # Draw to remaining points
                for pt in points[1:]:
                    px = inst_x + pt.get('x', 0)
                    py = inst_y + pt.get('y', 0)
                    pdf_x, pdf_y = self.to_pdf_coords_page(px, py, page_num)
                    path.lineTo(pdf_x, pdf_y)

                c.drawPath(path, stroke=1, fill=0)

            count += 1
            self.stats['symbols_drawn'] += 1

        return count

    def _render_labels(self, c: canvas.Canvas, page_num: int) -> int:
        """Render text labels for a page."""
        count = 0

        # Get primitives for this page
        primitives = self.primitives_by_page.get(page_num, [])

        # Track rendered positions to avoid duplicates
        rendered_positions = set()

        for prim in primitives:
            if prim.get('type') != 'text':
                continue

            geometry = prim.get('geometry', {})
            origin = geometry.get('origin', {})
            x = origin.get('x', 0)
            y = origin.get('y', 0)

            text_content = prim.get('text_content', '')
            if not text_content:
                continue

            # Deduplicate: skip if same text at same position (rounded to nearest 1000 units)
            pos_key = (round(x, -3), round(y, -3), text_content)
            if pos_key in rendered_positions:
                continue
            rendered_positions.add(pos_key)

            # Get style - resolve style_ref if inline style is empty
            style = prim.get('style', {})
            style_ref = prim.get('style_ref')
            if not style.get('font_size') and style_ref:
                resolved_style = self.data.get('styles', {}).get(style_ref, {})
                font_size = resolved_style.get('font_size', 7)
            else:
                font_size = style.get('font_size', 7)

            # Use white text for visibility on dark background
            extracted_color = style.get('font_color', '#000000')
            if extracted_color == '#000000':
                font_color = white  # White for visibility on dark background
            else:
                font_color = self.parse_color(extracted_color)

            # Get text rotation and justification from text_properties
            text_props = prim.get('text_properties', {})
            rotation = text_props.get('rotation', 0)

            # Resolve alignment: prefer explicit alignment string, else derive from justification code
            justification = text_props.get('justification', text_props.get('alignment'))
            if justification in [1, 3, 'center']:
                align = 'center'
            elif justification in [2, 'right']:
                align = 'right'
            else:
                align = 'left'

            # Set text style
            c.setFont("Helvetica", font_size)
            c.setFillColor(font_color)

            # Compute anchor-adjusted position before rotation
            pdf_x, pdf_y = self.to_pdf_coords_page(x, y, page_num)
            text_width = stringWidth(text_content, "Helvetica", font_size)
            if align == 'center':
                pdf_x -= text_width / 2.0
            elif align == 'right':
                pdf_x -= text_width

            if rotation != 0:
                # Rotate around the anchor point
                c.saveState()
                c.translate(pdf_x, pdf_y)
                c.rotate(rotation)  # ReportLab uses degrees CCW
                c.drawString(0, 0, text_content)
                c.restoreState()
            else:
                # Small baseline tweak for unrotated text
                pdf_y += font_size * 0.3
                c.drawString(pdf_x, pdf_y, text_content)

            count += 1
            self.stats['labels_drawn'] += 1

        return count


def main():
    """Main entry point."""
    # Default paths
    input_path = "full_design.json"
    output_path = "brain_board_rendered.pdf"

    # Parse command line arguments
    if len(sys.argv) > 1:
        input_path = sys.argv[1]
    if len(sys.argv) > 2:
        output_path = sys.argv[2]

    # Load design data
    print(f"Loading design data from: {input_path}")
    with open(input_path, 'r') as f:
        design_data = json.load(f)

    # Create renderer and generate PDF
    renderer = SchematicPDFRenderer(design_data)
    renderer.render_to_pdf(output_path)


if __name__ == "__main__":
    main()
