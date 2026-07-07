// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LiteLLMBar",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "LiteLLMBar", path: "Sources/LiteLLMBar")
    ]
)
