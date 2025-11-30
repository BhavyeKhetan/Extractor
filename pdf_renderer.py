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
from reportlab.lib.colors import HexColor, black, white


# Allegro dark theme colors
DARK_BACKGROUND = '#1a1a2e'  # Dark navy background
IC_BODY_FILL = '#404040'     # Dark gray for IC bodies
IC_BODY_STROKE = '#808080'   # Light gray for IC outlines


class SchematicPDFRenderer:
    """Renders extracted schematic data to PDF using ReportLab."""

    # Page dimensions (A4 landscape = 297mm x 210mm = 11.69" x 8.27")
    PAGE_WIDTH_INCHES = 11.69   # A4 width in landscape (297mm)
    PAGE_HEIGHT_INCHES = 8.27   # A4 height in landscape (210mm)

    # Page size in points (72 points per inch)
    PAGE_WIDTH = PAGE_WIDTH_INCHES * 72  # ~841 points
    PAGE_HEIGHT = PAGE_HEIGHT_INCHES * 72  # ~595 points

    # Internal units conversion (default - will be overridden by auto-fit)
    # Using 254000 as base scale for fallback
    UNITS_PER_INCH = 254000
    SCALE = 72.0 / UNITS_PER_INCH  # 0.000283 (fallback only)

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

        min_x, min_y, max_x, max_y = self._calculate_page_bounds(page_num)

        data_width = max_x - min_x
        data_height = max_y - min_y

        if data_width <= 0 or data_height <= 0:
            result = (self.SCALE, 0, 0)
            self._page_transforms[page_num] = result
            return result

        # Calculate scale to fit with 5% margin
        margin = 0.05
        usable_width = self.PAGE_WIDTH * (1 - 2 * margin)
        usable_height = self.PAGE_HEIGHT * (1 - 2 * margin)

        scale_x = usable_width / data_width
        scale_y = usable_height / data_height
        scale = min(scale_x, scale_y)

        # Calculate offset to shift content to positive coords and center
        # First shift to origin (add -min_x, -min_y)
        # Then add margin offset
        margin_offset_x = self.PAGE_WIDTH * margin / scale
        margin_offset_y = self.PAGE_HEIGHT * margin / scale

        # Center the content
        center_offset_x = (usable_width / scale - data_width) / 2
        center_offset_y = (usable_height / scale - data_height) / 2

        offset_x = -min_x + margin_offset_x + center_offset_x
        offset_y = -min_y + margin_offset_y + center_offset_y

        result = (scale, offset_x, offset_y)
        self._page_transforms[page_num] = result

        # Debug: print transform for each page
        print(f"    Page {page_num} auto-fit: bounds=({min_x:.0f}, {min_y:.0f}) to ({max_x:.0f}, {max_y:.0f}), "
              f"span=({data_width:.0f} x {data_height:.0f}), scale={scale:.6f}")

        return result

    def to_pdf_coords_page(self, x: float, y: float, page_num: int) -> Tuple[float, float]:
        """Transform coordinates with page-specific scale and offset."""
        scale, offset_x, offset_y = self._get_page_transform(page_num)
        pdf_x = (x + offset_x) * scale
        pdf_y = (y + offset_y) * scale
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
        print(f"Page size: {self.PAGE_WIDTH_INCHES}\" x {self.PAGE_HEIGHT_INCHES}\" (A4 Landscape)")
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

            # Set text style
            c.setFont("Helvetica", font_size)
            c.setFillColor(font_color)

            # Draw text with baseline adjustment
            pdf_x, pdf_y = self.to_pdf_coords_page(x, y, page_num)
            pdf_y += font_size * 0.3  # Baseline adjustment for overlap prevention
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
