import json
import re
import os
from pypdf import PdfReader

def extract_text_from_pdf(pdf_path):
    """Extracts text from all pages of the PDF."""
    text = ""
    try:
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            text += page.extract_text() + "\n"
    except Exception as e:
        print(f"Error reading PDF: {e}")
    return text

def extract_potential_refdes(text):
    """Finds potential RefDes patterns (e.g., U1, R10, C5) in text."""
    # Standard IPC prefixes: C, R, U, J, P, L, D, Q, Y, FB, SW, TP
    # Exclude A, B, etc. which are likely pin names
    pattern = r'\b((?:C|R|U|J|P|L|D|Q|Y|FB|SW|TP)[0-9]{1,4})\b'
    matches = re.findall(pattern, text)
    return set(matches)

def extract_potential_nets(text):
    """Finds potential Net Name patterns in text."""
    # Pattern: Uppercase, underscores, digits. Length > 3 to avoid noise.
    pattern = r'\b([A-Z0-9_]{3,})\b'
    matches = re.findall(pattern, text)
    # Filter out common noise
    noise = {'GND', 'VCC', 'PAGE', 'DATE', 'REV', 'SIZE', 'TITLE', 'DRAWN', 'CHECKED', 'BLOCK', 'SCHEMATIC'}
    return {m for m in matches if m not in noise and not m.isdigit()}

def verify_design(json_path, pdf_path, report_path):
    print(f"Verifying {json_path} against {pdf_path}...")
    
    # 1. Load JSON Data
    with open(json_path, 'r') as f:
        data = json.load(f)
    
    json_components = set()
    for comp in data.get('components_flat', []):
        if 'refdes' in comp:
            json_components.add(comp['refdes'])
            
    # Fallback to instances if components_flat is empty
    if not json_components:
        for inst in data.get('instances', []):
            # instances might be a dict or list? Let's check structure if needed.
            # Based on previous check, instances is a dict in python if loaded from json object?
            # Wait, checks showed instances count: 276.
            # Let's assume components_flat is the main source.
            pass
    json_nets = set(data.get('nets', {}).keys())
    
    # 2. Extract PDF Data
    pdf_text = extract_text_from_pdf(pdf_path)
    pdf_refdes = extract_potential_refdes(pdf_text)
    pdf_nets = extract_potential_nets(pdf_text)
    
    # 3. Compare
    # RefDes Verification
    found_refdes = sorted(list(pdf_refdes.intersection(json_components)))
    missing_refdes = sorted(list(pdf_refdes - json_components))
    
    # Net Verification
    # Note: PDF might have partial net names or labels that don't exactly match the full net name in JSON
    # So we check if the PDF token exists as a substring in any JSON net, or exact match
    found_nets = []
    missing_nets = []
    
    for p_net in pdf_nets:
        if p_net in json_nets:
            found_nets.append(p_net)
        else:
            # Loose matching: check if p_net is part of a longer net name in JSON
            # e.g. "DDR_DQ" in PDF might be "DDR_DQ<0>" in JSON
            if any(p_net in j_net for j_net in json_nets):
                found_nets.append(p_net)
            else:
                missing_nets.append(p_net)
                
    found_nets = sorted(list(set(found_nets)))
    missing_nets = sorted(list(set(missing_nets)))
    
    # 4. Generate Report
    with open(report_path, 'w') as f:
        f.write("# Design Verification Report (Run 2)\n\n")
        f.write(f"**JSON Source:** `{json_path}`\n")
        f.write(f"**PDF Ground Truth:** `{pdf_path}`\n\n")
        
        # Text Primitive Analysis
        json_text_prims = [p.get('text_content', '') for p in data.get('primitives', []) if p.get('type') == 'text']
        f.write("## Text Extraction Verification\n")
        f.write(f"- **Text Primitives in JSON:** {len(json_text_prims)}\n")
        f.write(f"- **Sample JSON Text:** `{', '.join(json_text_prims[:10])}`...\n\n")
        
        # Check if RefDes are in JSON text
        found_refdes_in_json_text = [rd for rd in json_components if rd in json_text_prims]
        f.write(f"### Internal Consistency (RefDes in JSON Text)\n")
        f.write(f"- **RefDes found in JSON Text:** {len(found_refdes_in_json_text)} / {len(json_components)}\n")
        f.write(f"- **Consistency Rate:** {len(found_refdes_in_json_text)/len(json_components)*100:.1f}%\n\n")

        f.write("## Summary\n")
        f.write(f"- **Components Found in PDF:** {len(pdf_refdes)}\n")
        f.write(f"- **Components Matched in JSON:** {len(found_refdes)}\n")
        f.write(f"- **Match Rate:** {len(found_refdes)/len(pdf_refdes)*100:.1f}%\n\n")
        
        f.write("## Component Verification\n")
        if missing_refdes:
            f.write("### Missing Components (Found in PDF but not in JSON)\n")
            f.write("These might be text artifacts or non-electrical components in the PDF.\n")
            f.write(f"`{', '.join(missing_refdes[:50])}`")
            if len(missing_refdes) > 50:
                f.write(" ... and more")
            f.write("\n\n")
        else:
            f.write("✅ **All components found in PDF were identified in the JSON!**\n\n")
            
        f.write("### Sample Matched Components\n")
        f.write(f"`{', '.join(found_refdes[:20])}`...\n\n")
        
        f.write("## Net Verification\n")
        f.write(f"- **Potential Nets Found in PDF:** {len(pdf_nets)}\n")
        f.write(f"- **Nets Matched in JSON:** {len(found_nets)}\n")
        f.write(f"- **Match Rate:** {len(found_nets)/len(pdf_nets)*100:.1f}%\n\n")
        
        if missing_nets:
            f.write("### Unmatched Net Labels\n")
            f.write("These might be generic text or labels not corresponding to electrical nets.\n")
            f.write(f"`{', '.join(missing_nets[:50])}`")
            if len(missing_nets) > 50:
                f.write(" ... and more")
            f.write("\n\n")
            
    print(f"Report generated at {report_path}")

if __name__ == "__main__":
    # Get absolute path to the directory containing this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(script_dir, "full_design.json")
    pdf_path = os.path.join(script_dir, "brain_board.pdf")
    report_path = os.path.join(script_dir, "verification_report_2.md")
    
    verify_design(json_path, pdf_path, report_path)
