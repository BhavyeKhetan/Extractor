import json
import os

def render_to_svg(json_path, output_dir):
    print(f"Loading {json_path}...")
    with open(json_path, 'r') as f:
        data = json.load(f)
        
    pages = data.get('pages', [])
    primitives = data.get('primitives', [])
    
    # Group primitives by page
    primitives_by_page = {}
    for p in primitives:
        page_idx = p.get('page_index')
        if page_idx not in primitives_by_page:
            primitives_by_page[page_idx] = []
        primitives_by_page[page_idx].append(p)
        
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    for page in pages:
        page_id = page.get('page_id')
        # page_id in json is string "1", "2"...
        # primitives use integer page_index 1, 2...
        try:
            page_idx = int(page_id)
        except:
            continue
            
        width = page['size']['width']
        height = page['size']['height']
        
        # Create SVG content
        # ViewBox should match the units. 
        # Units are mils? Or internal units?
        # Grid config says 100000 internal units = 1 inch.
        # Page size says unit "mils".
        # If width is 17000 mils, that's 17 inches.
        # Coordinates in primitives are likely internal units (e.g. 1905000).
        # 1905000 / 100000 = 19.05 inches.
        # So coordinates are in internal units.
        # Page size 17000 mils = 17 inches = 1,700,000 internal units?
        # Wait. 1 mil = 0.001 inch.
        # 17000 mils = 17 inches.
        # 17 inches * 100000 = 1,700,000.
        # But wire coordinates are like 1,905,000.
        # This is larger than 1,700,000.
        # Maybe the page size in JSON is wrong or I'm misinterpreting units.
        # Let's just use a large viewBox.
        
        svg_content = [
            f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{width}mil" height="{height}mil" '
            f'viewBox="0 0 {width*100} {height*100}">' # Guessing scale factor 100 (1 mil = 100 units?)
            # Wait, grid config: "mils_per_unit": 0.01.
            # So 1 unit = 0.01 mils.
            # 100 units = 1 mil.
            # So width in units = width_mils * 100.
        ]
        
        # Draw background
        svg_content.append(f'<rect x="0" y="0" width="{width*100}" height="{height*100}" fill="white"/>')
        
        page_prims = primitives_by_page.get(page_idx, [])
        print(f"Page {page_id}: {len(page_prims)} primitives")
        
        for prim in page_prims:
            ptype = prim.get('type')
            
            if ptype == 'line':
                points = prim.get('geometry', {}).get('points', [])
                if len(points) >= 2:
                    # Draw polyline
                    pts_str = " ".join([f"{p['x']},{p['y']}" for p in points])
                    color = prim.get('style', {}).get('line_color', '#000000')
                    width = prim.get('style', {}).get('line_width', 1) * 10 
                    svg_content.append(f'<polyline points="{pts_str}" stroke="{color}" stroke-width="{width}" fill="none"/>')
            
            elif ptype == 'text':
                geo = prim.get('geometry', {})
                origin = geo.get('origin', {'x':0, 'y':0})
                text = prim.get('text_content', '')
                # Escape XML chars
                text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                
                x = origin['x']
                y = origin['y']
                # Simple text rendering
                svg_content.append(f'<text x="{x}" y="{y}" font-family="Arial" font-size="2000" fill="blue">{text}</text>')
                    
            elif ptype == 'instance':
                # This might be the old unlinked instances if they still exist in primitives
                # But we should also check the top-level 'instances' list which now has coordinates
                pass

        # Draw instances from the top-level list
        # We need to filter by page
        page_instances = [inst for inst in data.get('instances', []) if str(inst.get('page_index')) == str(page_idx)]
        print(f"Page {page_id}: {len(page_instances)} instances")
        
        for inst in page_instances:
            x = inst.get('x')
            y = inst.get('y')
            refdes = inst.get('refdes', '?')
            
            if x is not None and y is not None:
                # Draw a box and label
                size = 5000
                svg_content.append(f'<rect x="{x}" y="{y}" width="{size}" height="{size}" stroke="red" stroke-width="20" fill="none"/>')
                svg_content.append(f'<text x="{x}" y="{y-1000}" font-family="Arial" font-size="3000" fill="red" font-weight="bold">{refdes}</text>')
                
        svg_content.append('</svg>')
        
        out_file = os.path.join(output_dir, f'page_{page_id}.svg')
        with open(out_file, 'w') as f:
            f.write("\n".join(svg_content))
            
    print(f"Rendered SVGs to {output_dir}")

if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(script_dir, "full_design.json")
    output_dir = os.path.join(script_dir, "rendered_output")
    render_to_svg(json_path, output_dir)
