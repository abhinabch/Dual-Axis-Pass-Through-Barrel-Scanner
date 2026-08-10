# Ground Truth Measurement Protocol — Barrel Internal Volume & Geometry

**Purpose:** Document a rigorous, repeatable process for acquiring physical ground-truth measurements (internal volume in Litres and physical dimensions) for scanned barrels.

---

## 1. Ground Truth Protocol Options

Depending on physical equipment availability, ground truth volume ($V_{\text{GT}}$) can be established using one of three protocols (listed in order of preference):

### 1.1 Method A: Mass-Based Water-Fill (Direct Volumetric — Ideal)
When a high-capacity industrial scale and water filling apparatus are available:
- Fill barrel with water, measure net water mass $M_{\text{water}}$ and temperature $T$.
- $V_{\text{GT}} = M_{\text{water}} / \rho_{\text{water}}(T) \times 1000$ (Litres).

### 1.2 Method B: Caliper Profile & Analytic Frustum Model (Practical Physical GT)
When water filling is unfeasible, ground truth volume is derived from precision caliper / tape measurements of the barrel geometry:
1. **Head Diameters ($D_{\text{top}}, D_{\text{bot}}$)**: Measured across internal or external croze bevels.
2. **Axial Span ($L$)**: Distance between inner head faces along the central axis.
3. **Bilge Diameter ($D_{\text{bilge}}$)**: Outer girth / inner diameter at the widest midpoint.
4. **Analytic Volume Formula**: Parabolic / circular frustum barrel profile integration:
   $$V_{\text{analytic}} = \frac{\pi L}{12} \left( 2 D_{\text{bilge}}^2 + D_{\text{head}}^2 \right) \;\; (\text{where } D_{\text{head}} = \frac{D_{\text{top}} + D_{\text{bot}}}{2})$$
   *Uncertainty*: $\pm 0.5 \text{ L}$ (caliper measurement noise floor).

### 1.3 Method C: Nominal Cooperage Specification Baseline
When physical measurements are unavailable prior to scanning:
- Use the Cooper's certified nominal capacity (e.g., standard 225.0 L Bordeaux Barrique, 228.0 L Burgundy Piece, 200.0 L Bourbon Barrel).
- *Uncertainty*: $\pm 2.5 \text{ L}$ ($\sim 1.0\%$ manufacturing variance across coopers).

---

## 2. Secondary Method: Caliper & Laser Profile Checks

As an independent cross-check against gross errors, physical external/internal dimensions are measured using precision calipers and internal depth rods:
- **Head Diameters ($D_{\text{top}}, D_{\text{bot}}$)**: Measured across top and bottom inner head discs.
- **Axial Length ($L$)**: Distance between inner head discs along axis.
- **Bilge Diameter ($D_{\text{bilge}}$)**: Measured at bung center.
- **Frustum/Ellipsoid Frustum Approximation Volume**: Used as a sanity filter ($\pm 2\%$ sanity check).

---

## 3. Surface Area Validation Strategy

> **Note on Surface Area**: Direct physical measurement of 3D internal surface area (including stave bevels, croze channels, and oak grain texture) is not feasible without destructive sectioning. Therefore, surface area is validated using geometric mesh quality proxies:
> - **Mesh Watertightness**: 100% closed, 2-manifold topology with 0 non-manifold edges.
> - **Point-to-Mesh Residual**: RMS residual of raw scan points against the reconstructed mesh.
> - **Degenerate Face Count**: 0 zero-area or self-intersecting faces.

---

## 4. Measurement Repeatability & Noise Floor Protocol

To establish the noise floor ($\sigma_{\text{GT}}$) of the ground-truth measurement process itself:
1. Select at least 3 representative barrels.
2. Repeat the water-fill measurement procedure 3 times per barrel (drain, dry, re-tare, re-fill).
3. Compute the standard deviation $\sigma_{\text{GT}}$ across repetitions for each barrel.
4. **Noise Floor Standard**: The overall ground truth uncertainty is defined as $\text{Max}(\sigma_{\text{GT}}, \text{Scale Resolution})$.
5. *Rule*: Any reconstruction pipeline claim with error smaller than $\sigma_{\text{GT}}$ is considered statistically indistinguishable from ground truth noise.

---

## 5. Record Keeping

All physical ground-truth measurements must be entered into `data/validation_set/validation_manifest.csv` with accompanying operator notes, water temperature, and scale calibration date.
