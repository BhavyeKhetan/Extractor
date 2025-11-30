# Design Verification Report (Run 2)

**JSON Source:** `/Users/bhavyekhetan/Downloads/Allegro_1/brain_board/full_design.json`
**PDF Ground Truth:** `/Users/bhavyekhetan/Downloads/Allegro_1/brain_board/brain_board.pdf`

## Text Extraction Verification
- **Text Primitives in JSON:** 2072
- **Sample JSON Text:** `P0_USB_DP, P0_USB_DN, P0_USB_ID, PS_POR_L, P1_USB_24M_REFCLK, P0_VBUS, P1_USB_DIR, VCC_BAR, VDD18_2, VDD18_1`...

### Internal Consistency (RefDes in JSON Text)
- **RefDes found in JSON Text:** 518 / 557
- **Consistency Rate:** 93.0%

## Summary
- **Components Found in PDF:** 346
- **Components Matched in JSON:** 269
- **Match Rate:** 77.7%

## Component Verification
### Missing Components (Found in PDF but not in JSON)
These might be text artifacts or non-electrical components in the PDF.
`C1, C312, C512, C8, C832, C92, C962, D0, D2, D7, D9, FB116, FB122, FB213, FB22, FB42, FB72, J17, J18, L15, L16, L3, R1101, R1141, R1161, R1901, R1911, R2061, R2081, R2091, R2101, R2111, R2121, R2131, R2141, R2151, R2161, R2211, R2261, R2271, R2281, R2291, R2301, R2311, R2321, R2341, R2351, R2401, R2411, R2821` ... and more

### Sample Matched Components
`C100, C101, C102, C103, C104, C105, C106, C107, C108, C109, C11, C110, C111, C112, C113, C12, C127, C128, C129, C13`...

## Net Verification
- **Potential Nets Found in PDF:** 1820
- **Nets Matched in JSON:** 174
- **Match Rate:** 9.6%

### Unmatched Net Labels
These might be generic text or labels not corresponding to electrical nets.
`0001LF, 002A, 0OUT, 1000P2, 10D0S9D0, 10LRCLK4MCLK3SPDIF, 11D0, 11FB213SW212, 11P8V, 12J12, 12R241, 12R3, 12R3812, 12R4, 12R82, 12R961, 12RESET_N4, 12U2, 13CLKS12CLK, 13OUT, 14R_EXT64HSYNC2VSYNC53CLK63DED, 16DDC18SDA17SCL2RES15CEC14CLK, 18TXC, 1C102, 1C260, 1C298, 1C32, 1C5, 1C59, 1C842, 1CENTER, 1CLG400C, 1FB17, 1FB18, 1FB3, 1FB52, 1HPD195V0, 1J10, 1J14, 1L11, 1P0V1P0VREG_EN4, 1P0V1P8V, 1P0VETH_RXD, 1P35V1P35V, 1P35V1P35V3P3V, 1P35V1P35VDDR_A, 1P35VVDD_DSP, 1P8V1P0V, 1P8V1P8V, 1P8V1P8V1P8V` ... and more

