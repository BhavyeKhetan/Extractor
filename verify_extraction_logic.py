import json
import sys
from collections import defaultdict

def verify_logic(json_path):
    print(f"Verifying logic for {json_path}...")
    
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: {json_path} not found.")
        return

    instances = data.get('instances', {})
    # If instances is a list (old format), convert to dict
    if isinstance(instances, list):
        print("Warning: 'instances' is a list. Converting to dict by refdes if possible.")
        temp_inst = {}
        for inst in instances:
            if 'refdes' in inst:
                temp_inst[inst['refdes']] = inst
        instances = temp_inst

    print(f"Total Instances Loaded: {len(instances)}")

    # 1. Block Distribution
    block_counts = defaultdict(int)
    for refdes, inst in instances.items():
        block = inst.get('block', 'unknown')
        block_counts[block] += 1
    
    print("\n--- Block Distribution ---")
    expected_blocks = {
        'zynq_block': 201,
        'dsp_block': 121,
        'mgmt_block': 91,
        'gige_block': 41,
        'hdmi_block_2': 38,
        'usb_block': 30,
        'ddr3_block': 25,
        'reusable_usb_conn': 9,
        'brain_board': 1
    }
    
    for block, count in block_counts.items():
        print(f"{block}: {count}")
        if block in expected_blocks:
            diff = count - expected_blocks[block]
            if abs(diff) > 5: # Allow small variance
                print(f"  [WARN] Expected ~{expected_blocks[block]}, got {count}")
            else:
                print(f"  [OK] Matches expected.")
                
    # 2. Page Assignment Verification
    print("\n--- Page Assignment Verification ---")
    page_counts = defaultdict(int)
    for refdes, inst in instances.items():
        page = inst.get('page_index')
        if page is not None:
            page_counts[page] += 1
            
    expected_pages = {
        11: 43,
        16: 38,
        17: 48,
        18: 29
    }
    
    for page, expected in expected_pages.items():
        count = page_counts.get(page, 0)
        print(f"Page {page}: {count} instances")
        if abs(count - expected) > 5:
             print(f"  [WARN] Expected ~{expected}, got {count}")
        else:
             print(f"  [OK] Matches expected.")

    # 3. Hierarchical Instance Handling
    print("\n--- Hierarchical Instance Handling ---")
    test_cases = {
        'R2': 'reusable_usb_conn',
        'R6': 'reusable_usb_conn',
        'R7': 'reusable_usb_conn',
        'U17': 'reusable_usb_conn',
        'C2': 'reusable_usb_conn', 
        'J10': 'hdmi_block_2'
    }
    
    for refdes, expected_block in test_cases.items():
        if refdes in instances:
            inst = instances[refdes]
            actual_block = inst.get('block')
            has_pos = inst.get('x') is not None and inst.get('y') is not None
            
            print(f"{refdes}: Block={actual_block}, HasPosition={has_pos}")
            
            if actual_block != expected_block:
                print(f"  [FAIL] Expected block {expected_block}, got {actual_block}")
            elif not has_pos:
                print(f"  [FAIL] Missing position data")
            else:
                print(f"  [PASS]")
        else:
            print(f"{refdes}: [FAIL] Not found in instances")

    # 4. Coordinate System Check
    print("\n--- Coordinate System Check ---")
    min_x, max_x = float('inf'), float('-inf')
    min_y, max_y = float('inf'), float('-inf')
    
    out_of_bounds = 0
    # Page size B (Landscape) is 17000 x 11000 mils
    # Internal units: 1 unit = 0.01 mils
    limit_x = 1700000
    limit_y = 1100000
    
    for refdes, inst in instances.items():
        x = inst.get('x')
        y = inst.get('y')
        if x is not None and y is not None:
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            
            if not (0 <= x <= limit_x and 0 <= y <= limit_y):
                 out_of_bounds += 1

    print(f"Coordinate Range: X[{min_x}, {max_x}], Y[{min_y}, {max_y}]")
    print(f"Instances out of bounds (0-{limit_x}, 0-{limit_y}): {out_of_bounds}")
    
    if max_y > limit_y and max_y <= 1700000:
        print("  [INFO] Y-coordinates fit within 1700000 (Portrait Mode?)")

    # 5. Wire Extraction
    print("\n--- Wire Extraction ---")
    # Wires are in primitives with shape_type='wire'
    primitives = data.get('primitives', [])
    wires = [p for p in primitives if p.get('shape_type') == 'wire']
    
    print(f"Total Wires (Primitives): {len(wires)}")
    if len(wires) < 800:
        print(f"  [WARN] Expected ~890 wires, got {len(wires)}")
    else:
        print(f"  [OK] Wire count reasonable.")

if __name__ == "__main__":
    verify_logic("full_design.json")
