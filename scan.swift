// Decodes barcodes with macOS Vision and prints JSON:
// [{"file":..., "symbology":..., "payload":..., "box":[x0,y0,x1,y1]}]  (box in pixels, origin top-left)
import Foundation
import Vision
import AppKit

var out: [[String: Any]] = []
for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let req = VNDetectBarcodesRequest()
    try? VNImageRequestHandler(cgImage: cg).perform([req])
    let w = Double(cg.width), h = Double(cg.height)
    for r in req.results ?? [] {
        let b = r.boundingBox
        out.append([
            "file": path,
            "symbology": r.symbology.rawValue.replacingOccurrences(of: "VNBarcodeSymbology", with: ""),
            "payload": r.payloadStringValue ?? "",
            "box": [b.minX * w, (1 - b.maxY) * h, b.maxX * w, (1 - b.minY) * h].map { $0.rounded() },
        ])
    }
}
let data = try! JSONSerialization.data(withJSONObject: out, options: [])
print(String(data: data, encoding: .utf8)!)
