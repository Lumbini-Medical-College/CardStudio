// Person cut-out with macOS Vision (no downloads): cutout <in-image> <out.png>
// Writes a PNG with a transparent background, cropped to the person.
import Foundation
import Vision
import CoreImage
import AppKit

let args = CommandLine.arguments
guard args.count == 3, let src = CIImage(contentsOf: URL(fileURLWithPath: args[1])) else {
    FileHandle.standardError.write("usage: cutout <in> <out.png>\n".data(using: .utf8)!); exit(2)
}
let oriented = src.oriented(forExifOrientation: Int32((src.properties["Orientation"] as? Int) ?? 1))
let handler = VNImageRequestHandler(ciImage: oriented)
var mask: CIImage?

// Prefer the general foreground mask (macOS 14+), fall back to person segmentation.
if #available(macOS 14.0, *) {
    let req = VNGenerateForegroundInstanceMaskRequest()
    if (try? handler.perform([req])) != nil, let r = req.results?.first,
       let buf = try? r.generateScaledMaskForImage(forInstances: r.allInstances, from: handler) {
        mask = CIImage(cvPixelBuffer: buf)
    }
}
if mask == nil {
    let req = VNGeneratePersonSegmentationRequest()
    req.qualityLevel = .accurate
    if (try? handler.perform([req])) != nil, let buf = req.results?.first?.pixelBuffer {
        let m = CIImage(cvPixelBuffer: buf)
        mask = m.transformed(by: CGAffineTransform(scaleX: oriented.extent.width / m.extent.width,
                                                   y: oriented.extent.height / m.extent.height))
    }
}
guard let m = mask else { FileHandle.standardError.write("no person found\n".data(using: .utf8)!); exit(1) }

let blend = CIFilter(name: "CIBlendWithMask", parameters: [
    kCIInputImageKey: oriented, kCIInputBackgroundImageKey: CIImage.empty(), kCIInputMaskImageKey: m])!.outputImage!
let ctx = CIContext()
guard let cg = ctx.createCGImage(blend, from: oriented.extent) else { exit(1) }
let rep = NSBitmapImageRep(cgImage: cg)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[2]))
