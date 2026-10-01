import Foundation
import SoundAnalysis

final class Results: NSObject, SNResultsObserving {
    var scores: [String: Double] = [:]

    func request(_ request: SNRequest, didProduce result: SNResult) {
        guard let result = result as? SNClassificationResult else { return }
        for candidate in result.classifications.prefix(8) {
            scores[candidate.identifier] = max(scores[candidate.identifier] ?? 0, candidate.confidence)
        }
    }

    func request(_ request: SNRequest, didFailWithError error: Error) {
        fputs("Analysis error: \(error)\n", stderr)
    }
}

guard CommandLine.arguments.count == 2 else {
    fputs("Usage: classify <audio-file.wav>\n", stderr)
    exit(2)
}

do {
    let analyzer = try SNAudioFileAnalyzer(url: URL(fileURLWithPath: CommandLine.arguments[1]))
    let request = try SNClassifySoundRequest(classifierIdentifier: .version1)
    let observer = Results()
    try analyzer.add(request, withObserver: observer)
    analyzer.analyze()
    let sorted = observer.scores.sorted { $0.value > $1.value }.prefix(5)
    let output = sorted.map { ["identifier": $0.key, "confidence": $0.value] as [String: Any] }
    let data = try JSONSerialization.data(withJSONObject: output)
    print(String(data: data, encoding: .utf8) ?? "[]")
} catch {
    fputs("\(error)\n", stderr)
    exit(1)
}
