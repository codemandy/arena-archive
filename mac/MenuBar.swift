// Menu bar drop tray.
//
// Drag files, images, links or text onto the menu bar icon and a channel
// list opens underneath it. Drop straight onto a channel to file the items
// there, or drop onto the icon to hold them and click a channel later. Items
// go through the running archive server, the same way the web page adds them.

import AppKit
import UniformTypeIdentifiers

let accent = NSColor(red: 0xe6 / 255, green: 1, blue: 0x3f / 255, alpha: 1)

struct ChannelSummary: Decodable {
    let id: Int
    let title: String
    let category: String
    let favorite: Bool
    let blockCount: Int
}

// MARK: - Dropped items

enum TrayItem {
    /// `temporary` files were written by the app (browser images, promised
    /// files) and are deleted once filed. Other files belong to the user.
    case file(URL, source: String?, temporary: Bool)
    case link(URL, title: String?)
    case text(String)

    var label: String {
        switch self {
        case .file(let url, _, _): return url.lastPathComponent
        case .link(let url, let title): return title ?? url.host ?? url.absoluteString
        case .text(let text): return "“" + text.trimmingCharacters(in: .whitespacesAndNewlines).prefix(40) + "”"
        }
    }
}

enum DropReader {
    static let urlName = NSPasteboard.PasteboardType("public.url-name")
    static let types: [NSPasteboard.PasteboardType] = [.fileURL, .URL, .string, .png, .tiff]
        + NSFilePromiseReceiver.readableDraggedTypes.map { NSPasteboard.PasteboardType($0) }
    static let queue: OperationQueue = {
        let queue = OperationQueue()
        queue.qualityOfService = .userInitiated
        return queue
    }()

    /// Reads a drag pasteboard, most specific first: Finder files, promised
    /// files (browsers, Photos, Mail), raw image data, links, then plain text.
    static func read(_ pasteboard: NSPasteboard, completion: @escaping ([TrayItem]) -> Void) {
        if let files = pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !files.isEmpty {
            return completion(files.map { .file($0, source: nil, temporary: false) })
        }
        let webURL = (pasteboard.readObjects(forClasses: [NSURL.self]) as? [URL])?.first { !$0.isFileURL }
        // The pasteboard is only readable during the drop, so work out the
        // fallback now in case the promised files never arrive.
        let fallback = readInline(pasteboard, webURL: webURL)
        guard let promises = pasteboard.readObjects(forClasses: [NSFilePromiseReceiver.self]) as? [NSFilePromiseReceiver],
              !promises.isEmpty else { return completion(fallback) }
        receive(promises, source: webURL?.absoluteString) { items in completion(items.isEmpty ? fallback : items) }
    }

    static func readInline(_ pasteboard: NSPasteboard, webURL: URL?) -> [TrayItem] {
        let imageData = pasteboard.data(forType: .png)
            ?? pasteboard.data(forType: .tiff).flatMap { NSBitmapImageRep(data: $0)?.representation(using: .png, properties: [:]) }
        if let imageData, let file = try? temporaryFolder().appendingPathComponent("Dropped image.png"),
           (try? imageData.write(to: file)) != nil {
            return [.file(file, source: webURL?.absoluteString, temporary: true)]
        }
        if let webURL {
            return [.link(webURL, title: pasteboard.string(forType: urlName))]
        }
        guard let text = pasteboard.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return [] }
        if let url = URL(string: text), ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
            return [.link(url, title: nil)]
        }
        return [.text(text)]
    }

    static func receive(_ receivers: [NSFilePromiseReceiver], source: String?, completion: @escaping ([TrayItem]) -> Void) {
        guard let folder = try? temporaryFolder() else { return completion([]) }
        let lock = NSLock()
        var items: [TrayItem] = []
        var pending = receivers.count
        var finished = false
        func finish() {
            lock.lock()
            defer { lock.unlock() }
            guard !finished else { return }
            finished = true
            let result = items
            DispatchQueue.main.async { completion(result) }
        }
        for receiver in receivers {
            let expected = max(1, receiver.fileNames.count)
            var received = 0
            receiver.receivePromisedFiles(atDestination: folder, options: [:], operationQueue: queue) { url, error in
                lock.lock()
                if error == nil { items.append(item(forReceived: url, source: source)) }
                received += 1
                if received == expected { pending -= 1 }
                let done = pending == 0
                lock.unlock()
                if done { finish() }
            }
        }
        // Some apps never deliver; don't hold the drop forever.
        DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: finish)
    }

    /// Safari hands over links as .webloc files; file those as links.
    static func item(forReceived url: URL, source: String?) -> TrayItem {
        if url.pathExtension.lowercased() == "webloc",
           let data = try? Data(contentsOf: url),
           let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
           let link = (plist["URL"] as? String).flatMap(URL.init(string:)) {
            try? fm.removeItem(at: url)
            return .link(link, title: url.deletingPathExtension().lastPathComponent)
        }
        return .file(url, source: source, temporary: true)
    }

    static func temporaryFolder() throws -> URL {
        let folder = fm.temporaryDirectory.appendingPathComponent("ArenaDrops/\(UUID().uuidString)")
        try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder
    }
}

// MARK: - Server client

struct ArchiveClient {
    let base: URL

    func channels() async throws -> [ChannelSummary] {
        struct Response: Decodable { let channels: [ChannelSummary] }
        let (data, response) = try await URLSession.shared.data(from: base.appendingPathComponent("api/channels"))
        try check(data, response)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Response.self, from: data).channels
    }

    func add(_ item: TrayItem, to channel: Int) async throws {
        let request: URLRequest
        switch item {
        case .file(let url, let source, _):
            var isFolder: ObjCBool = false
            guard fm.fileExists(atPath: url.path, isDirectory: &isFolder) else { throw SyncError("\(url.lastPathComponent) is gone.") }
            if isFolder.boolValue { throw SyncError("\(url.lastPathComponent) is a folder. Drop the files inside it instead.") }
            var fields = ["channel_id": String(channel)]
            if let source { fields["source_url"] = source }
            request = try upload(url, fields: fields)
        case .link(let url, let title):
            request = form("create-block", ["channel_id": String(channel), "type": "link", "title": title ?? url.host ?? url.absoluteString,
                                            "content": "", "source_url": url.absoluteString])
        case .text(let text):
            let firstLine = text.split(whereSeparator: \.isNewline).first.map(String.init) ?? text
            request = form("create-block", ["channel_id": String(channel), "type": "text",
                                            "title": String(firstLine.prefix(60)), "content": text])
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        try check(data, response)
    }

    func form(_ path: String, _ fields: [String: String]) -> URLRequest {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        let body = fields.map { "\($0.key)=\($0.value.addingPercentEncoding(withAllowedCharacters: allowed) ?? "")" }.joined(separator: "&")
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data(body.utf8)
        return request
    }

    func upload(_ file: URL, fields: [String: String]) throws -> URLRequest {
        let boundary = "ArenaArchive-\(UUID().uuidString)"
        var body = Data()
        for (name, value) in fields {
            body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8))
        }
        let name = file.lastPathComponent
        let filename = name.allSatisfy(\.isASCII)
            ? "filename=\"\(name.replacingOccurrences(of: "\"", with: "_"))\""
            : "filename*=UTF-8''\(name.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "file")"
        let type = UTType(filenameExtension: file.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; \(filename)\r\nContent-Type: \(type)\r\n\r\n".utf8))
        body.append(try Data(contentsOf: file))
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var request = URLRequest(url: base.appendingPathComponent("upload-file"))
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        return request
    }

    /// The server answers errors with an HTML notice; pull out its message.
    func check(_ data: Data, _ response: URLResponse) throws {
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard !(200..<400).contains(status) else { return }
        if status == 403 { throw SyncError("The archive is read-only right now.") }
        let page = String(decoding: data, as: UTF8.self)
        let paragraph = page.range(of: "<p>").flatMap { start in
            page.range(of: "</p>", range: start.upperBound..<page.endIndex).map { String(page[start.upperBound..<$0.lowerBound]) }
        }
        throw SyncError(paragraph ?? "The archive answered with error \(status).")
    }
}

// MARK: - Views

final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

final class ChannelRow: NSView {
    var onDrop: (NSPasteboard) -> Void = { _ in }
    var onClick: () -> Void = {}
    private let title = NSTextField(labelWithString: "")
    private let meta = NSTextField(labelWithString: "")
    private var hovering = false { didSet { updateColors() } }
    private var dropping = false { didSet { updateColors() } }

    init(channel: ChannelSummary) {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 5
        title.stringValue = channel.title
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.lineBreakMode = .byTruncatingTail
        let category = channel.category.isEmpty ? "Uncategorized" : channel.category
        meta.stringValue = ((channel.favorite ? "★ " : "") + "\(category) · \(channel.blockCount) blocks").uppercased()
        meta.font = .monospacedSystemFont(ofSize: 9.5, weight: .regular)
        meta.lineBreakMode = .byTruncatingTail
        for label in [title, meta] { label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal) }
        let stack = NSStackView(views: [title, meta])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 1
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -10),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 6),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -6),
        ])
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self))
        registerForDraggedTypes(DropReader.types)
        updateColors()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    // The whole row is one target for clicks and drops, labels included.
    override func hitTest(_ point: NSPoint) -> NSView? { frame.contains(point) ? self : nil }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseEntered(with event: NSEvent) { hovering = true }
    override func mouseExited(with event: NSEvent) { hovering = false }
    override func mouseDown(with event: NSEvent) {}
    override func mouseUp(with event: NSEvent) {
        if bounds.contains(convert(event.locationInWindow, from: nil)) { onClick() }
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        dropping = true
        return .copy
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        autoscroll(toward: sender.draggingLocation)
        return .copy
    }

    override func draggingExited(_ sender: NSDraggingInfo?) { dropping = false }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        dropping = false
        onDrop(sender.draggingPasteboard)
        return true
    }

    /// You can't scroll while dragging, so hovering near the top or bottom
    /// edge of the list scrolls it.
    private func autoscroll(toward windowPoint: NSPoint) {
        guard let scroll = enclosingScrollView, let document = scroll.documentView else { return }
        let clip = scroll.contentView
        let point = clip.convert(windowPoint, from: nil)
        var origin = clip.bounds.origin
        if point.y < clip.bounds.minY + 36 { origin.y -= 14 } else if point.y > clip.bounds.maxY - 36 { origin.y += 14 } else { return }
        origin.y = min(max(0, origin.y), max(0, document.frame.height - clip.bounds.height))
        clip.scroll(to: origin)
        scroll.reflectScrolledClipView(clip)
    }

    private func updateColors() {
        layer?.backgroundColor = dropping ? accent.cgColor
            : hovering ? NSColor.labelColor.withAlphaComponent(0.07).cgColor : NSColor.clear.cgColor
        title.textColor = dropping ? .black : .labelColor
        meta.textColor = dropping ? NSColor.black.withAlphaComponent(0.6) : .secondaryLabelColor
    }
}

func eyebrow(_ text: String) -> NSTextField {
    let label = NSTextField(labelWithString: "")
    label.attributedStringValue = NSAttributedString(string: text.uppercased(), attributes: [
        .font: NSFont.monospacedSystemFont(ofSize: 10, weight: .medium), .foregroundColor: NSColor.secondaryLabelColor, .kern: 1.2,
    ])
    return label
}

final class ChannelPicker: NSViewController, NSSearchFieldDelegate {
    var onDrop: (Int, NSPasteboard) -> Void = { _, _ in }
    var onPick: (Int) -> Void = { _ in }
    var onClear: () -> Void = {}
    var onOpen: () -> Void = {}
    var onOptions: (NSView) -> Void = { _ in }

    private let search = NSSearchField()
    private let trayLabel = NSTextField(wrappingLabelWithString: "")
    private let clearButton = NSButton(title: "Clear", target: nil, action: nil)
    private let list = NSStackView()
    private let scroll = NSScrollView()
    private let status = NSTextField(labelWithString: "")
    private var channels: [ChannelSummary] = []
    private var recent: [Int] = []
    private var placeholder: String? = "Opening archive…"

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: 480))

        let options = NSButton(image: NSImage(systemSymbolName: "ellipsis.circle", accessibilityDescription: "Options")!,
                               target: self, action: #selector(showOptions(_:)))
        options.isBordered = false
        let header = NSStackView(views: [eyebrow(appName), NSView(), options])

        trayLabel.font = .systemFont(ofSize: 12)
        trayLabel.textColor = .secondaryLabelColor
        trayLabel.maximumNumberOfLines = 3
        trayLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        clearButton.bezelStyle = .inline
        clearButton.controlSize = .small
        clearButton.target = self
        clearButton.action = #selector(clear)
        let tray = NSStackView(views: [trayLabel, clearButton])
        tray.alignment = .top

        search.placeholderString = "Find a channel"
        search.delegate = self
        search.sendsSearchStringImmediately = true

        list.orientation = .vertical
        list.alignment = .leading
        list.spacing = 0
        list.translatesAutoresizingMaskIntoConstraints = false
        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(list)
        scroll.documentView = document
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)

        status.font = .monospacedSystemFont(ofSize: 10, weight: .regular)
        status.textColor = .secondaryLabelColor
        status.lineBreakMode = .byTruncatingTail
        status.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let open = NSButton(title: "Open Archive", target: self, action: #selector(openArchive))
        open.controlSize = .small
        let footer = NSStackView(views: [status, NSView(), open])

        let root = NSStackView(views: [header, tray, search, scroll, footer])
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        root.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            root.topAnchor.constraint(equalTo: view.topAnchor),
            root.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            view.widthAnchor.constraint(equalToConstant: 340),
            view.heightAnchor.constraint(equalToConstant: 480),
            document.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            document.trailingAnchor.constraint(equalTo: scroll.contentView.trailingAnchor),
            document.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            list.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            list.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            list.topAnchor.constraint(equalTo: document.topAnchor),
            list.bottomAnchor.constraint(equalTo: document.bottomAnchor),
        ] + [header, tray, search, scroll, footer].map { $0.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -24) })
        showTray([])
        rebuild()
    }

    func update(channels: [ChannelSummary], recent: [Int]) {
        self.channels = channels
        self.recent = recent
        placeholder = nil
        rebuild()
    }

    func show(placeholder: String) {
        self.placeholder = placeholder
        rebuild()
    }

    func say(_ message: String) {
        status.stringValue = message
        status.toolTip = message
    }

    func showTray(_ labels: [String]) {
        _ = view
        clearButton.isHidden = labels.isEmpty
        if labels.isEmpty {
            trayLabel.stringValue = "Drop onto a channel below to file it there, or onto the menu bar icon to hold it and pick a channel later."
        } else {
            let count = labels.count == 1 ? "1 item held" : "\(labels.count) items held"
            trayLabel.stringValue = "\(count). Click a channel to file \(labels.count == 1 ? "it" : "them").\n" + labels.joined(separator: ", ")
        }
    }

    func focusSearch() {
        view.window?.makeFirstResponder(search)
    }

    func reset() {
        search.stringValue = ""
        rebuild()
    }

    private func rebuild() {
        list.arrangedSubviews.forEach { $0.removeFromSuperview() }
        if let placeholder { return addNote(placeholder) }
        let term = search.stringValue.trimmingCharacters(in: .whitespaces).lowercased()
        if term.isEmpty {
            addSection("Recent", recent.compactMap { id in channels.first { $0.id == id } })
            addSection("Favorites", channels.filter(\.favorite))
            addSection("All channels", channels)
            if channels.isEmpty { addNote("No channels yet. Create one in the archive window.") }
        } else {
            let matches = matchingChannels(term)
            matches.isEmpty ? addNote("No channels match “\(search.stringValue)”.") : addSection("Matches", matches)
        }
        scroll.contentView.scroll(to: .zero)
        scroll.reflectScrolledClipView(scroll.contentView)
    }

    private func matchingChannels(_ term: String) -> [ChannelSummary] {
        channels.filter { "\($0.title) \($0.category)".lowercased().contains(term) }
    }

    private func addSection(_ title: String, _ rows: [ChannelSummary]) {
        guard !rows.isEmpty else { return }
        let label = eyebrow(title)
        list.addArrangedSubview(label)
        list.setCustomSpacing(4, after: label)
        if list.arrangedSubviews.count > 1 { list.setCustomSpacing(14, after: list.arrangedSubviews[list.arrangedSubviews.count - 2]) }
        for channel in rows {
            let row = ChannelRow(channel: channel)
            row.onDrop = { [weak self] in self?.onDrop(channel.id, $0) }
            row.onClick = { [weak self] in self?.onPick(channel.id) }
            list.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: list.widthAnchor).isActive = true
        }
    }

    private func addNote(_ text: String) {
        let note = NSTextField(wrappingLabelWithString: text)
        note.textColor = .secondaryLabelColor
        list.addArrangedSubview(note)
        note.widthAnchor.constraint(equalTo: list.widthAnchor).isActive = true
    }

    func controlTextDidChange(_ notification: Notification) { rebuild() }

    // Return files into (or opens) the first match.
    func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
        guard selector == #selector(NSResponder.insertNewline(_:)) else { return false }
        let term = search.stringValue.trimmingCharacters(in: .whitespaces).lowercased()
        if let first = term.isEmpty ? nil : matchingChannels(term).first { onPick(first.id) }
        return true
    }

    @objc private func clear() { onClear() }
    @objc private func openArchive() { onOpen() }
    @objc private func showOptions(_ sender: NSButton) { onOptions(sender) }
}

/// Sits on top of the status item button so it can take drops.
final class StatusDropView: NSView {
    unowned let controller: MenuBarController

    init(controller: MenuBarController) {
        self.controller = controller
        super.init(frame: .zero)
        registerForDraggedTypes(DropReader.types)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func mouseDown(with event: NSEvent) {
        event.modifierFlags.contains(.control) ? controller.showOptions(in: self) : controller.togglePopover()
    }

    override func rightMouseDown(with event: NSEvent) { controller.showOptions(in: self) }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        controller.dragHovering(true)
        return .copy
    }

    override func draggingExited(_ sender: NSDraggingInfo?) { controller.dragHovering(false) }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        controller.dragHovering(false)
        controller.drop(sender.draggingPasteboard, into: nil)
        return true
    }
}

// MARK: - Controller

final class MenuBarController: NSObject, NSPopoverDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let popover = NSPopover()
    let picker = ChannelPicker()
    let archiveURL: () -> URL?
    let showArchive: (Int?) -> Void
    let archiveChanged: (Int) -> Void
    var channels: [ChannelSummary] = []
    var tray: [TrayItem] = [] { didSet { updateTray() } }
    var closeTimer: DispatchWorkItem?

    var recent: [Int] {
        get { UserDefaults.standard.array(forKey: "recentDropChannels") as? [Int] ?? [] }
        set { UserDefaults.standard.set(Array(newValue.prefix(4)), forKey: "recentDropChannels") }
    }

    var movesFiles: Bool {
        get { UserDefaults.standard.bool(forKey: "moveDroppedFiles") }
        set { UserDefaults.standard.set(newValue, forKey: "moveDroppedFiles") }
    }

    init(archiveURL: @escaping () -> URL?, showArchive: @escaping (Int?) -> Void, archiveChanged: @escaping (Int) -> Void) {
        self.archiveURL = archiveURL
        self.showArchive = showArchive
        self.archiveChanged = archiveChanged
        super.init()
        popover.behavior = .transient
        popover.contentViewController = picker
        popover.delegate = self
        if let button = statusItem.button {
            button.image = icon(filled: false)
            button.imagePosition = .imageLeading
            button.target = self
            button.action = #selector(togglePopover)
            let drop = StatusDropView(controller: self)
            drop.frame = button.bounds
            drop.autoresizingMask = [.width, .height]
            button.addSubview(drop)
        }
        picker.onDrop = { [weak self] channel, pasteboard in self?.drop(pasteboard, into: channel) }
        picker.onPick = { [weak self] channel in self?.pick(channel) }
        picker.onClear = { [weak self] in self?.clearTray() }
        picker.onOpen = { [weak self] in self?.popover.performClose(nil); self?.showArchive(nil) }
        picker.onOptions = { [weak self] view in self?.showOptions(in: view) }
    }

    func icon(filled: Bool) -> NSImage? {
        let image = NSImage(systemSymbolName: filled ? "tray.and.arrow.down.fill" : "tray.and.arrow.down", accessibilityDescription: appName)
        image?.isTemplate = true
        return image
    }

    // MARK: Popover

    @objc func togglePopover() {
        if popover.isShown { return popover.performClose(nil) }
        // Activate so the search field takes typing.
        NSApp.activate(ignoringOtherApps: true)
        showPopover()
        picker.focusSearch()
    }

    func showPopover() {
        cancelAutoClose()
        guard !popover.isShown, let button = statusItem.button else { return }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        button.highlight(true)
        refreshChannels()
    }

    func popoverDidClose(_ notification: Notification) {
        statusItem.button?.highlight(false)
        picker.reset()
    }

    func dragHovering(_ hovering: Bool) {
        statusItem.button?.image = icon(filled: hovering)
        if hovering { showPopover() }
    }

    func refreshChannels() {
        guard let base = archiveURL() else { return picker.show(placeholder: "Opening archive…") }
        Task { @MainActor in
            do {
                channels = try await ArchiveClient(base: base).channels()
                picker.update(channels: channels, recent: recent)
            } catch {
                picker.show(placeholder: "Could not load channels. \(error.localizedDescription)")
            }
        }
    }

    func showOptions(in view: NSView) {
        let menu = NSMenu()
        let move = NSMenuItem(title: "Move Files to Trash After Filing", action: #selector(toggleMoveFiles), keyEquivalent: "")
        move.target = self
        move.state = movesFiles ? .on : .off
        move.toolTip = "Off: the archive keeps a copy and your original stays put."
        menu.addItem(move)
        menu.addItem(.separator())
        let open = NSMenuItem(title: "Open Archive Window", action: #selector(openArchive), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
        menu.addItem(NSMenuItem(title: "Quit \(appName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: view.bounds.height + 4), in: view)
    }

    @objc func toggleMoveFiles() { movesFiles.toggle() }

    @objc func openArchive() {
        popover.performClose(nil)
        showArchive(nil)
    }

    // MARK: Filing

    func drop(_ pasteboard: NSPasteboard, into channel: Int?) {
        DropReader.read(pasteboard) { [weak self] items in
            guard let self else { return }
            guard !items.isEmpty else { return self.picker.say("Nothing to add from that drop.") }
            if let channel {
                self.file(items, into: channel) { _ in }
            } else {
                self.tray += items
                self.showPopover()
                self.picker.say("")
            }
        }
    }

    /// Clicking a channel files whatever is held, or opens the channel.
    func pick(_ channel: Int) {
        guard !tray.isEmpty else {
            popover.performClose(nil)
            return showArchive(channel)
        }
        let held = tray
        tray = []
        // Anything that fails stays held so you can try another channel.
        file(held, into: channel) { [weak self] failed in self?.tray = failed + (self?.tray ?? []) }
    }

    func clearTray() {
        tray.forEach { if case .file(let url, _, true) = $0 { try? fm.removeItem(at: url) } }
        tray = []
        picker.say("")
    }

    func file(_ items: [TrayItem], into channel: Int, completion: @escaping ([TrayItem]) -> Void) {
        guard let base = archiveURL() else {
            picker.say("The archive isn't open yet.")
            return completion(items)
        }
        let name = channels.first { $0.id == channel }?.title ?? "the channel"
        picker.say("Adding \(count(items.count)) to \(name)…")
        let client = ArchiveClient(base: base)
        let movesFiles = movesFiles
        Task { @MainActor in
            var failed: [TrayItem] = []
            var firstError: Error?
            for item in items {
                do {
                    try await client.add(item, to: channel)
                    if case .file(let url, _, let temporary) = item {
                        if temporary { try? fm.removeItem(at: url) } else if movesFiles { NSWorkspace.shared.recycle([url], completionHandler: nil) }
                    }
                } catch {
                    failed.append(item)
                    firstError = firstError ?? error
                }
            }
            let added = items.count - failed.count
            if added > 0 {
                recent = [channel] + recent.filter { $0 != channel }
                archiveChanged(channel)
                refreshChannels()
            }
            if let firstError {
                picker.say("Added \(added) of \(items.count) to \(name). \(firstError.localizedDescription)")
            } else {
                picker.say("Added \(count(added)) to \(name).")
                if tray.isEmpty { scheduleAutoClose() }
            }
            completion(failed)
        }
    }

    func count(_ n: Int) -> String { n == 1 ? "1 item" : "\(n) items" }

    // After a drag from another app the popover would otherwise stay open
    // until the next click.
    func scheduleAutoClose() {
        cancelAutoClose()
        let work = DispatchWorkItem { [weak self] in self?.popover.performClose(nil) }
        closeTimer = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6, execute: work)
    }

    func cancelAutoClose() {
        closeTimer?.cancel()
        closeTimer = nil
    }

    func updateTray() {
        statusItem.button?.title = tray.isEmpty ? "" : " \(tray.count)"
        picker.showTray(tray.map(\.label))
    }
}
