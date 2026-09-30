// Are.na Archive — a small native shell around server.py.
//
// Build with ./mac/build.sh (Command Line Tools only, no Xcode).
//
// Local mode serves an archive folder on this Mac (archive.db + assets/) in
// place, and remembers the folder you pick on first launch.
//
// File › Move Archive to iCloud Drive… switches to iCloud mode (the `useICloud`
// default). Data then lives in iCloud Drive › CHANNEL: the app works on a
// local copy of archive.db and writes it back to iCloud, because iCloud syncs
// whole files and would otherwise clobber a live SQLite database. A lock file
// in the iCloud folder tells the other Mac the archive is in use.

import AppKit
import CryptoKit
import SQLite3
import WebKit

let appName = "CHANNEL"
let syncInterval: TimeInterval = 20
let fm = FileManager.default
var useICloud: Bool {
    get { UserDefaults.standard.bool(forKey: "useICloud") }
    set { UserDefaults.standard.set(newValue, forKey: "useICloud") }
}

enum Paths {
    static let home = fm.homeDirectoryForCurrentUser
    static let environment = ProcessInfo.processInfo.environment
    // The ARENA_* overrides exist for testing against a scratch folder.
    static let iCloudDrive = environment["ARENA_ICLOUD_DRIVE"].map { URL(fileURLWithPath: $0) }
        ?? home.appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs")
    static let cloud = iCloudDrive.appendingPathComponent("CHANNEL")
    static let cloudDB = cloud.appendingPathComponent("archive.db")
    static let cloudAssets = cloud.appendingPathComponent("assets")
    static let lock = cloud.appendingPathComponent("lock.json")
    // One file per Mac, so the two Macs never write the same file.
    static let versions = cloud.appendingPathComponent("versions")
    static let support = environment["ARENA_SUPPORT"].map { URL(fileURLWithPath: $0) }
        ?? home.appendingPathComponent("Library/Application Support/ArenaArchive")
    static let localDB = support.appendingPathComponent("archive.db")
    static let readOnlyDB = support.appendingPathComponent("archive-readonly.db")
    static let state = support.appendingPathComponent("sync-state.json")
    static let upload = support.appendingPathComponent("upload.db")
    static let log = support.appendingPathComponent("server.log")
    static let thumbs = support.appendingPathComponent("thumbs")
}

// MARK: - Files

func sha256(_ url: URL) -> String? {
    guard let data = try? Data(contentsOf: url, options: .mappedIfSafe) else { return nil }
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

/// Copies a SQLite database with the online backup API, so a copy taken while
/// the server is writing is still consistent.
func backupDatabase(from source: URL, to destination: URL) throws {
    try? fm.removeItem(at: destination)
    var src: OpaquePointer?
    var dst: OpaquePointer?
    defer { sqlite3_close(src); sqlite3_close(dst) }
    guard sqlite3_open_v2(source.path, &src, SQLITE_OPEN_READONLY, nil) == SQLITE_OK,
          sqlite3_open_v2(destination.path, &dst, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nil) == SQLITE_OK,
          let backup = sqlite3_backup_init(dst, "main", src, "main") else {
        throw SyncError("Could not open \(source.lastPathComponent) for copying.")
    }
    var result: Int32
    repeat {
        result = sqlite3_backup_step(backup, -1)
        if result == SQLITE_BUSY || result == SQLITE_LOCKED { usleep(100_000) }
    } while result == SQLITE_OK || result == SQLITE_BUSY || result == SQLITE_LOCKED
    sqlite3_backup_finish(backup)
    guard result == SQLITE_DONE else { throw SyncError("Copying the database failed (SQLite \(result)).") }
}

/// Asks iCloud to download a file and waits until the local copy is current.
func waitForDownload(_ url: URL, timeout: TimeInterval = 30) {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        var url = url
        url.removeAllCachedResourceValues()
        let status = try? url.resourceValues(forKeys: [.ubiquitousItemDownloadingStatusKey]).ubiquitousItemDownloadingStatus
        if status == nil || status == .current { return }
        try? fm.startDownloadingUbiquitousItem(at: url)
        Thread.sleep(forTimeInterval: 0.5)
    }
}

func coordinatedRead<T>(_ url: URL, _ body: (URL) throws -> T) throws -> T {
    var coordinationError: NSError?
    var result: Result<T, Error>?
    NSFileCoordinator().coordinate(readingItemAt: url, options: [], error: &coordinationError) { url in
        result = Result { try body(url) }
    }
    if let coordinationError { throw coordinationError }
    return try result!.get()
}

func coordinatedWrite(_ url: URL, _ body: (URL) throws -> Void) throws {
    var coordinationError: NSError?
    var result: Result<Void, Error>?
    NSFileCoordinator().coordinate(writingItemAt: url, options: .forReplacing, error: &coordinationError) { url in
        result = Result { try body(url) }
    }
    if let coordinationError { throw coordinationError }
    try result!.get()
}

/// Atomically moves `source` over `destination` (same volume, rename(2)).
func replace(_ destination: URL, with source: URL) throws {
    guard rename(source.path, destination.path) == 0 else {
        throw SyncError("Could not replace \(destination.lastPathComponent): \(String(cString: strerror(errno)))")
    }
}

struct SyncError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

// MARK: - Lock

struct Lock: Codable {
    let machineID: String
    let machineName: String
    let heartbeat: Date
}

enum Machine {
    static let name = Host.current().localizedName ?? "Another Mac"
    static let id: String = {
        if let id = UserDefaults.standard.string(forKey: "machineID") { return id }
        let id = UUID().uuidString
        UserDefaults.standard.set(id, forKey: "machineID")
        return id
    }()
}

func jsonEncoder() -> JSONEncoder {
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    return encoder
}

func jsonDecoder() -> JSONDecoder {
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    return decoder
}

func readLock() -> Lock? {
    guard fm.fileExists(atPath: Paths.lock.path) else { return nil }
    waitForDownload(Paths.lock, timeout: 5)
    return try? coordinatedRead(Paths.lock) { try jsonDecoder().decode(Lock.self, from: Data(contentsOf: $0)) }
}

func writeLock() {
    let data = try? jsonEncoder().encode(Lock(machineID: Machine.id, machineName: Machine.name, heartbeat: Date()))
    try? coordinatedWrite(Paths.lock) { try data?.write(to: $0, options: .atomic) }
}

func releaseLock() {
    guard readLock()?.machineID == Machine.id else { return }
    try? coordinatedWrite(Paths.lock) { try fm.removeItem(at: $0) }
}

// MARK: - App version

/// What build.sh wrote into Info.plist: the build number is the commit count.
enum AppVersion {
    static let info = Bundle.main.infoDictionary ?? [:]
    static let build = Int(info["CFBundleVersion"] as? String ?? "") ?? 0
    static let commit = info["CHANNELCommit"] as? String ?? "unknown"
    static let sourcePath = (info["CHANNELSourcePath"] as? String).map { URL(fileURLWithPath: $0) }
    static var updateScript: URL? {
        guard let script = sourcePath?.appendingPathComponent("mac/update.sh"),
              fm.isExecutableFile(atPath: script.path) else { return nil }
        return script
    }
}

/// What the update button in the page shows.
struct UpdateStatus: Codable {
    var state: String  // checking, current, available, error, updating
    var count = 0
    var detail = ""
    var dirty = false
}

func runGit(_ arguments: [String], in repo: URL, timeout: TimeInterval = 30) -> (ok: Bool, output: String) {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/git")
    process.arguments = ["-C", repo.path] + arguments
    var environment = ProcessInfo.processInfo.environment
    environment["GIT_TERMINAL_PROMPT"] = "0"  // never hang on a password prompt
    process.environment = environment
    let output = Pipe()
    process.standardOutput = output
    process.standardError = output
    guard (try? process.run()) != nil else { return (false, "git is not installed") }
    let deadline = Date().addingTimeInterval(timeout)
    while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
    if process.isRunning {
        process.terminate()
        return (false, "timed out")
    }
    let text = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    return (process.terminationStatus == 0, text.trimmingCharacters(in: .whitespacesAndNewlines))
}

/// Fetches from GitHub and counts commits the installed build doesn't have yet.
/// That also catches a pull that was never rebuilt.
func checkForUpdates() -> UpdateStatus {
    guard let repo = AppVersion.sourcePath, fm.fileExists(atPath: repo.appendingPathComponent(".git").path) else {
        return UpdateStatus(state: "error", detail: "Can't find the project folder this app was built from.")
    }
    let fetch = runGit(["fetch", "--quiet"], in: repo)
    guard fetch.ok else {
        return UpdateStatus(state: "error", detail: "Couldn't reach GitHub: \(fetch.output.split(separator: "\n").last ?? "")")
    }
    let upstream = runGit(["rev-parse", "--abbrev-ref", "@{u}"], in: repo).output
    let installed = runGit(["cat-file", "-e", "\(AppVersion.commit)^{commit}"], in: repo).ok ? AppVersion.commit : "HEAD"
    let count = Int(runGit(["rev-list", "--count", "\(installed)..@{u}"], in: repo).output) ?? 0
    let dirty = !runGit(["status", "--porcelain", "--untracked-files=no"], in: repo).output.isEmpty
    let time = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .short)
    guard count > 0 else {
        return UpdateStatus(state: "current", detail: "Build \(AppVersion.build) (\(AppVersion.commit)) is the latest on \(upstream). Checked \(time).", dirty: dirty)
    }
    let changes = runGit(["log", "--format=• %s", "-8", "\(installed)..@{u}"], in: repo).output
    var detail = "\(count) new on \(upstream):\n\(changes)"
    if dirty { detail += "\n\nThe project folder has uncommitted changes. Commit or stash them before updating." }
    return UpdateStatus(state: "available", count: count, detail: detail, dirty: dirty)
}

struct VersionRecord: Codable {
    let machineID: String
    let machineName: String
    let build: Int
    let commit: String
    let updated: Date
}

/// Records this Mac's version in iCloud and returns another Mac's record if
/// that Mac runs a newer build.
func publishVersionAndFindNewer() -> VersionRecord? {
    try? fm.createDirectory(at: Paths.versions, withIntermediateDirectories: true)
    let mine = VersionRecord(machineID: Machine.id, machineName: Machine.name, build: AppVersion.build,
                             commit: AppVersion.commit, updated: Date())
    let data = try? jsonEncoder().encode(mine)
    try? coordinatedWrite(Paths.versions.appendingPathComponent("\(Machine.id).json")) { try data?.write(to: $0, options: .atomic) }
    let files = (try? fm.contentsOfDirectory(at: Paths.versions, includingPropertiesForKeys: nil)) ?? []
    return files
        .filter { $0.pathExtension == "json" && $0.deletingPathExtension().lastPathComponent != Machine.id }
        .compactMap { file -> VersionRecord? in
            waitForDownload(file, timeout: 5)
            return try? coordinatedRead(file) { try jsonDecoder().decode(VersionRecord.self, from: Data(contentsOf: $0)) }
        }
        .filter { $0.build > AppVersion.build }
        .max { $0.build < $1.build }
}

// MARK: - Sync

/// Hashes of the local and iCloud databases the last time they were in sync.
/// A side whose hash moved since then has changes the other side lacks.
struct SyncState: Codable {
    var localHash: String
    var cloudHash: String
}

enum Sync {
    static func loadState() -> SyncState? {
        try? JSONDecoder().decode(SyncState.self, from: Data(contentsOf: Paths.state))
    }

    static func save(_ state: SyncState) {
        try? JSONEncoder().encode(state).write(to: Paths.state, options: .atomic)
    }

    static func cloudHash() throws -> String? {
        waitForDownload(Paths.cloudDB)
        return try coordinatedRead(Paths.cloudDB) { sha256($0) }
    }

    /// Replaces the local working copy with the iCloud database.
    static func pull() throws {
        let staging = Paths.support.appendingPathComponent("download.db")
        try? fm.removeItem(at: staging)
        try coordinatedRead(Paths.cloudDB) { try fm.copyItem(at: $0, to: staging) }
        let hash = sha256(staging) ?? ""
        try? fm.removeItem(at: Paths.support.appendingPathComponent("archive.db-journal"))
        try replace(Paths.localDB, with: staging)
        save(SyncState(localHash: hash, cloudHash: hash))
    }

    enum PushResult { case unchanged, pushed, conflict(String) }

    /// Writes local changes to iCloud unless another Mac changed it first,
    /// in which case the local copy is saved next to it as a conflict file.
    static func pushIfChanged() throws -> PushResult {
        guard let localHash = sha256(Paths.localDB) else { return .unchanged }
        let state = loadState()
        if state?.localHash == localHash { return .unchanged }
        if let state, try cloudHash() != state.cloudHash {
            return .conflict(try saveConflictCopy())
        }
        try backupDatabase(from: Paths.localDB, to: Paths.upload)
        let uploadHash = sha256(Paths.upload) ?? ""
        try coordinatedWrite(Paths.cloudDB) { try replace($0, with: Paths.upload) }
        save(SyncState(localHash: localHash, cloudHash: uploadHash))
        return .pushed
    }

    static func saveConflictCopy() throws -> String {
        let stamp = ISO8601DateFormatter.string(from: Date(), timeZone: .current, formatOptions: [.withFullDate, .withTime])
        let name = "archive conflict \(Machine.name) \(stamp).db"
        try backupDatabase(from: Paths.localDB, to: Paths.upload)
        try coordinatedWrite(Paths.cloud.appendingPathComponent(name)) { try replace($0, with: Paths.upload) }
        return name
    }

    /// Brings the local copy up to date on launch. Returns a message when
    /// both sides had diverged and a conflict copy was saved.
    static func reconcileOnLaunch() throws -> String? {
        guard fm.fileExists(atPath: Paths.localDB.path), let state = loadState() else {
            try pull()
            return nil
        }
        let localChanged = sha256(Paths.localDB) != state.localHash
        let cloudChanged = try cloudHash() != state.cloudHash
        switch (localChanged, cloudChanged) {
        case (false, false): return nil
        case (false, true): try pull(); return nil
        case (true, false): _ = try pushIfChanged(); return nil
        case (true, true):
            let name = try saveConflictCopy()
            try pull()
            return "Unsynced changes on this Mac clashed with newer changes from iCloud. They were saved as “\(name)” in iCloud Drive › CHANNEL."
        }
    }

    /// Copies an existing archive folder (archive.db + assets/) into iCloud Drive.
    static func migrate(from folder: URL, progress: (String) -> Void) throws {
        let sourceDB = folder.appendingPathComponent("archive.db")
        let sourceAssets = folder.appendingPathComponent("assets")
        try fm.createDirectory(at: Paths.cloud, withIntermediateDirectories: true)
        progress("Copying archive.db to iCloud Drive…")
        try backupDatabase(from: sourceDB, to: Paths.upload)
        try coordinatedWrite(Paths.cloudDB) { try replace($0, with: Paths.upload) }
        if fm.fileExists(atPath: sourceAssets.path) {
            let files = fm.enumerator(at: sourceAssets, includingPropertiesForKeys: [.isRegularFileKey])?
                .compactMap { $0 as? URL }.filter { (try? $0.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true } ?? []
            for (index, file) in files.enumerated() {
                let relative = file.path.dropFirst(sourceAssets.path.count + 1)
                let target = Paths.cloudAssets.appendingPathComponent(String(relative))
                if fm.fileExists(atPath: target.path) { continue }
                try fm.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
                try fm.copyItem(at: file, to: target)
                if index % 25 == 0 { progress("Copying assets to iCloud Drive… \(index + 1) of \(files.count)") }
            }
        }
        try? fm.removeItem(at: Paths.state)
        try? fm.removeItem(at: Paths.localDB)
    }
}

// MARK: - Server

final class Server {
    let process = Process()
    let port: Int

    init(database: URL, assets: URL = Paths.cloudAssets, thumbs: URL = Paths.thumbs, readOnly: Bool = false) throws {
        port = Server.freePort()
        guard let script = Bundle.main.url(forResource: "server", withExtension: "py") else {
            throw SyncError("server.py is missing from the app bundle. Rebuild with mac/build.sh.")
        }
        let python = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
            .first { fm.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [script.path]
        var environment = ProcessInfo.processInfo.environment
        environment["ARENA_DATABASE"] = database.path
        environment["ARENA_ASSETS"] = assets.path
        environment["ARENA_THUMBS"] = thumbs.path
        environment["ARENA_READONLY"] = readOnly ? "1" : "0"
        environment["ARENA_PARENT_PID"] = String(getpid())
        environment["PORT"] = String(port)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"  // keep the signed bundle untouched
        process.environment = environment
        fm.createFile(atPath: Paths.log.path, contents: nil)
        let log = try FileHandle(forWritingTo: Paths.log)
        process.standardOutput = log
        process.standardError = log
        try process.run()
    }

    var url: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    func waitUntilReady() throws {
        let probe = url.appendingPathComponent("style.css")
        for _ in 0..<100 {
            if !process.isRunning { break }
            if (try? Data(contentsOf: probe)) != nil { return }
            Thread.sleep(forTimeInterval: 0.1)
        }
        let log = (try? String(contentsOf: Paths.log, encoding: .utf8)) ?? ""
        throw SyncError("The archive server did not start.\n\n\(log.suffix(800))")
    }

    func stop() {
        if process.isRunning { process.terminate() }
    }

    static func freePort() -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        address.sin_port = 0
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                _ = bind(fd, $0, length)
                _ = getsockname(fd, $0, &length)
            }
        }
        return Int(UInt16(bigEndian: address.sin_port))
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    let webView = ArchiveWebView(frame: .zero, configuration: WKWebViewConfiguration())
    let status = NSTextField(labelWithString: "Opening archive…")
    let statusView = NSView()
    var server: Server?
    var readOnly = false
    var timer: Timer?
    var titleObservation: NSKeyValueObservation?
    var menuBar: MenuBarController!
    let work = DispatchQueue(label: "archive.sync")
    var updateStatus: UpdateStatus?
    var updateCheckedAt: Date?
    var checkingForUpdates = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = makeMenu()
        makeWindow()
        webView.configuration.userContentController.add(self, name: "channel")
        menuBar = MenuBarController(archiveURL: { [weak self] in self?.server?.url },
                                    showArchive: { [weak self] in self?.showArchive(channel: $0) },
                                    archiveChanged: { [weak self] in self?.archiveChanged(channel: $0) })
        NSApp.activate(ignoringOtherApps: true)
        try? fm.createDirectory(at: Paths.support, withIntermediateDirectories: true)

        useICloud ? startICloud() : openLocal()
    }

    /// Opens the iCloud archive, first copying the local archive folder there
    /// when iCloud Drive doesn't have one yet.
    func startICloud() {
        guard fm.fileExists(atPath: Paths.iCloudDrive.path) else {
            fail("iCloud Drive is off", "Turn on iCloud Drive in System Settings › Apple Account › iCloud, then open the app again.")
            return
        }
        if fm.fileExists(atPath: Paths.cloudDB.path) {
            checkLockAndOpen()
        } else if let folder = localFolder, fm.fileExists(atPath: folder.appendingPathComponent("archive.db").path) {
            migrate(from: folder)
        } else {
            askForExistingArchive()
        }
    }

    @objc func moveToICloud() {
        guard fm.fileExists(atPath: Paths.iCloudDrive.path) else {
            return showAlert("iCloud Drive is off", "Turn on iCloud Drive in System Settings › Apple Account › iCloud, then try again.")
        }
        let joining = fm.fileExists(atPath: Paths.cloudDB.path)
        let alert = NSAlert()
        if joining {
            alert.messageText = "Use the archive in iCloud Drive?"
            alert.informativeText = "iCloud Drive › CHANNEL already has an archive, probably from your other Mac. CHANNEL will use it from now on. The folder on this Mac is left as it is."
            alert.addButton(withTitle: "Use iCloud Archive")
        } else {
            alert.messageText = "Move your archive to iCloud Drive?"
            alert.informativeText = "archive.db and assets/ are copied from \(localFolder?.path ?? "your archive folder") to iCloud Drive › CHANNEL, and CHANNEL uses that copy from now on. The originals stay where they are. Uploading can take a while."
            alert.addButton(withTitle: "Move to iCloud")
        }
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        useICloud = true
        NSApp.mainMenu = makeMenu()
        server?.stop()
        server = nil
        showStatus(joining ? "Opening the iCloud archive…" : "Copying to iCloud Drive…")
        startICloud()
    }

    // Closing the window leaves the menu bar tray running.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { window.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        timer?.invalidate()
        if useICloud && server != nil && !readOnly {
            work.sync {
                if case .conflict(let name) = (try? Sync.pushIfChanged()) ?? .unchanged {
                    NSLog("Saved conflict copy \(name)")
                }
                releaseLock()
            }
        }
        server?.stop()
        return .terminateNow
    }

    // MARK: Local folder

    var localFolder: URL? {
        get { UserDefaults.standard.url(forKey: "archiveFolder") }
        set { UserDefaults.standard.set(newValue, forKey: "archiveFolder") }
    }

    /// Serves archive.db and assets/ straight from a folder on this Mac.
    func openLocal() {
        guard let folder = localFolder, fm.fileExists(atPath: folder.appendingPathComponent("archive.db").path) else {
            return chooseLocalFolder()
        }
        status.stringValue = "Opening archive…"
        server?.stop()
        work.async {
            do {
                let server = try Server(database: folder.appendingPathComponent("archive.db"),
                                        assets: folder.appendingPathComponent("assets"),
                                        thumbs: folder.appendingPathComponent("thumbs"))
                try server.waitUntilReady()
                DispatchQueue.main.async {
                    self.server = server
                    self.showWebView()
                }
            } catch {
                DispatchQueue.main.async { self.fail("Could not open the archive", error.localizedDescription) }
            }
        }
    }

    @objc func chooseLocalFolder() {
        let panel = NSOpenPanel()
        panel.message = "Choose the folder that contains archive.db and assets/."
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.prompt = "Open Archive"
        panel.directoryURL = localFolder
        guard panel.runModal() == .OK, let folder = panel.url else {
            if server == nil { NSApp.terminate(nil) }
            return
        }
        guard fm.fileExists(atPath: folder.appendingPathComponent("archive.db").path) else {
            let alert = NSAlert()
            alert.messageText = "No archive.db in that folder"
            alert.informativeText = "Pick the folder where you ran the importer."
            alert.runModal()
            return chooseLocalFolder()
        }
        localFolder = folder
        openLocal()
    }

    // MARK: Launch steps

    func askForExistingArchive() {
        let alert = NSAlert()
        alert.messageText = "Move your archive to iCloud Drive"
        alert.informativeText = "Choose the folder that contains archive.db and assets/. They are copied to iCloud Drive › CHANNEL, and the originals stay where they are."
        alert.addButton(withTitle: "Choose Folder…")
        alert.addButton(withTitle: "Quit")
        guard alert.runModal() == .alertFirstButtonReturn else { return NSApp.terminate(nil) }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.prompt = "Copy to iCloud"
        panel.directoryURL = localFolder
        guard panel.runModal() == .OK, let folder = panel.url else { return askForExistingArchive() }
        guard fm.fileExists(atPath: folder.appendingPathComponent("archive.db").path) else {
            showAlert("No archive.db in that folder", "Pick the folder where you ran the importer.")
            return askForExistingArchive()
        }
        migrate(from: folder)
    }

    func migrate(from folder: URL) {
        work.async {
            do {
                try Sync.migrate(from: folder) { message in DispatchQueue.main.async { self.status.stringValue = message } }
                DispatchQueue.main.async { self.checkLockAndOpen() }
            } catch {
                DispatchQueue.main.async { self.fail("Could not copy the archive", error.localizedDescription) }
            }
        }
    }

    func checkLockAndOpen() {
        status.stringValue = "Checking iCloud…"
        work.async {
            let lock = readLock()
            DispatchQueue.main.async {
                if let lock, lock.machineID != Machine.id {
                    let ago = RelativeDateTimeFormatter().localizedString(for: lock.heartbeat, relativeTo: Date())
                    let alert = NSAlert()
                    alert.messageText = "The archive is open on “\(lock.machineName)”"
                    alert.informativeText = "Last active \(ago). Editing here at the same time can create conflict copies. If that Mac is asleep or the app crashed there, it's safe to take over."
                    alert.addButton(withTitle: "Take Over")
                    alert.addButton(withTitle: "Open Read-Only")
                    alert.addButton(withTitle: "Quit")
                    switch alert.runModal() {
                    case .alertFirstButtonReturn: self.open(readOnly: false)
                    case .alertSecondButtonReturn: self.open(readOnly: true)
                    default: NSApp.terminate(nil)
                    }
                } else {
                    self.open(readOnly: false)
                }
            }
        }
    }

    func open(readOnly: Bool) {
        self.readOnly = readOnly
        status.stringValue = "Syncing with iCloud…"
        work.async {
            do {
                var message: String?
                let database: URL
                if readOnly {
                    waitForDownload(Paths.cloudDB)
                    try? fm.removeItem(at: Paths.readOnlyDB)
                    try coordinatedRead(Paths.cloudDB) { try fm.copyItem(at: $0, to: Paths.readOnlyDB) }
                    database = Paths.readOnlyDB
                } else {
                    writeLock()
                    message = try Sync.reconcileOnLaunch()
                    database = Paths.localDB
                }
                self.downloadAssets()
                let server = try Server(database: database, readOnly: readOnly)
                try server.waitUntilReady()
                let newer = publishVersionAndFindNewer()
                DispatchQueue.main.async {
                    self.server = server
                    self.showWebView()
                    if !readOnly { self.startSyncTimer() }
                    if let message {
                        self.showAlert("Sync conflict", message)
                    } else if let newer {
                        self.showUpdateNotice(newer)
                    }
                }
            } catch {
                DispatchQueue.main.async { self.fail("Could not open the archive", error.localizedDescription) }
            }
        }
    }

    /// Nudges iCloud to keep every asset on disk, so images don't go missing
    /// when "Optimize Mac Storage" has evicted them.
    func downloadAssets() {
        DispatchQueue.global(qos: .utility).async {
            let files = fm.enumerator(at: Paths.cloudAssets, includingPropertiesForKeys: nil)
            while let file = files?.nextObject() as? URL {
                try? fm.startDownloadingUbiquitousItem(at: file)
            }
        }
    }

    // MARK: Updates

    /// Messages from the page's update button: status (cached for 10 minutes),
    /// check (always asks GitHub), update (runs mac/update.sh).
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "status":
            if let updateStatus, let updateCheckedAt, Date().timeIntervalSince(updateCheckedAt) < 600 {
                sendUpdateStatus(updateStatus)
            } else {
                refreshUpdateStatus()
            }
        case "check":
            refreshUpdateStatus()
        case "update":
            if updateStatus?.dirty == true {
                showAlert("Commit your changes first", "The project folder has uncommitted changes, so the update would stop. Commit and push them (or stash them), then update.")
            } else {
                confirmUpdate()
            }
        default:
            break
        }
    }

    func refreshUpdateStatus() {
        guard !checkingForUpdates else { return sendUpdateStatus(UpdateStatus(state: "checking")) }
        checkingForUpdates = true
        sendUpdateStatus(UpdateStatus(state: "checking"))
        DispatchQueue.global(qos: .utility).async {
            let status = checkForUpdates()
            DispatchQueue.main.async {
                self.checkingForUpdates = false
                self.updateStatus = status
                self.updateCheckedAt = Date()
                self.sendUpdateStatus(status)
            }
        }
    }

    func sendUpdateStatus(_ status: UpdateStatus) {
        guard let data = try? JSONEncoder().encode(status), let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.channelUpdate && window.channelUpdate(\(json))")
    }

    func showUpdateNotice(_ newer: VersionRecord) {
        let alert = NSAlert()
        alert.messageText = "“\(newer.machineName)” has a newer CHANNEL"
        alert.informativeText = "That Mac runs build \(newer.build) (\(newer.commit)). This one runs build \(AppVersion.build) (\(AppVersion.commit)). Update to keep both Macs in step. If the newer build isn't pushed to GitHub yet, push it from that Mac first."
        alert.addButton(withTitle: "Update Now")
        alert.addButton(withTitle: "Later")
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.runUpdate() }
        }
    }

    @objc func confirmUpdate() {
        let alert = NSAlert()
        alert.messageText = "Update CHANNEL?"
        alert.informativeText = "CHANNEL quits, pulls the latest version from GitHub, rebuilds and reopens. Terminal shows the progress. This Mac runs build \(AppVersion.build) (\(AppVersion.commit))."
        alert.addButton(withTitle: "Update")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.runUpdate() }
        }
    }

    /// Runs mac/update.sh in Terminal. The script quits this app first, so the
    /// archive is written back to iCloud before the new build replaces it.
    func runUpdate() {
        guard let script = AppVersion.updateScript else {
            let path = AppVersion.sourcePath?.path ?? "the project folder"
            return showAlert("Can't find the update script", "Expected mac/update.sh in \(path). Clone the repo there, or run ./mac/update.sh from wherever it lives.")
        }
        let terminal = Process()
        terminal.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        terminal.arguments = ["-a", "Terminal", script.path]
        do {
            try terminal.run()
            sendUpdateStatus(UpdateStatus(state: "updating"))
        } catch {
            showAlert("Could not start the update", error.localizedDescription)
        }
    }

    // MARK: Periodic sync

    func startSyncTimer() {
        timer = Timer.scheduledTimer(withTimeInterval: syncInterval, repeats: true) { [weak self] _ in self?.syncTick() }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in
            self?.syncTick()
        }
    }

    func syncTick() {
        guard !readOnly else { return }
        work.async {
            if let lock = readLock(), lock.machineID != Machine.id {
                let result = try? Sync.pushIfChanged()
                DispatchQueue.main.async { self.lostLock(to: lock.machineName, result: result) }
                return
            }
            writeLock()
            do {
                if case .conflict(let name) = try Sync.pushIfChanged() {
                    DispatchQueue.main.async {
                        self.switchToReadOnly()
                        self.showAlert("The archive changed on another Mac", "Your latest changes were saved as “\(name)” in iCloud Drive › CHANNEL. Quit and reopen to load the newest version.")
                    }
                }
            } catch {
                NSLog("Sync failed: \(error.localizedDescription)")
            }
        }
    }

    func lostLock(to machine: String, result: Sync.PushResult?) {
        switchToReadOnly()
        var detail = "Changes you make here now won't be saved. Quit and reopen to take it back."
        if case .conflict(let name) = result { detail = "Your last changes were saved as “\(name)”. " + detail }
        showAlert("The archive was opened on “\(machine)”", "This window is read-only now. " + detail)
    }

    func switchToReadOnly() {
        guard !readOnly else { return }
        readOnly = true
        timer?.invalidate()
        server?.stop()
        work.async {
            guard let server = try? Server(database: Paths.localDB, readOnly: true), (try? server.waitUntilReady()) != nil else { return }
            DispatchQueue.main.async {
                self.server = server
                self.webView.load(URLRequest(url: server.url))
                self.updateTitle()
            }
        }
    }

    // MARK: Window

    func makeWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = appName
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(red: 0xf5 / 255, green: 0xf5 / 255, blue: 0xf0 / 255, alpha: 1)
        window.center()
        window.setFrameAutosaveName("ArchiveWindow")
        window.tabbingMode = .disallowed
        status.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        status.textColor = .secondaryLabelColor
        status.alignment = .center
        status.translatesAutoresizingMaskIntoConstraints = false
        statusView.addSubview(status)
        NSLayoutConstraint.activate([
            status.centerXAnchor.constraint(equalTo: statusView.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: statusView.centerYAnchor),
            status.widthAnchor.constraint(lessThanOrEqualTo: statusView.widthAnchor, constant: -40),
        ])
        window.contentView = statusView
        window.makeKeyAndOrderFront(nil)
    }

    func showStatus(_ message: String) {
        status.stringValue = message
        window.contentView = statusView
        window.makeKeyAndOrderFront(nil)
    }

    func showWebView() {
        webView.onFiles = { [weak self] urls, point in self?.fileDroppedInPage(urls, at: point) ?? false }
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        window.styleMask.remove(.fullSizeContentView)
        window.contentView = webView
        titleObservation = webView.observe(\.title) { [weak self] _, _ in self?.updateTitle() }
        if let server { webView.load(URLRequest(url: server.url)) }
    }

    /// Finder's right-click › Open With › CHANNEL, and `open -a CHANNEL <file>`:
    /// hold the files and ask which channel they belong in.
    func application(_ application: NSApplication, open urls: [URL]) {
        let files = urls.filter { $0.isFileURL }
        guard !files.isEmpty else { return }
        NSApp.activate(ignoringOtherApps: true)
        let pasteboard = NSPasteboard(name: .init("studio.oxoy.arena-archive.open"))
        pasteboard.clearContents()
        pasteboard.writeObjects(files.map { $0 as NSURL })
        menuBar.drop(pasteboard, into: nil)
    }

    /// Files dropped on the page are filed into the channel under the pointer and
    /// the originals go to the Trash, so a drop moves them into the archive.
    func fileDroppedInPage(_ urls: [URL], at point: NSPoint) -> Bool {
        guard let server, !readOnly else { return false }
        let zoom = webView.pageZoom
        let x = point.x / zoom
        let y = (webView.bounds.height - point.y) / zoom
        webView.evaluateJavaScript("window.channelAt ? window.channelAt(\(x), \(y)) : ''") { [weak self] value, _ in
            guard let self, let channel = Int(String(describing: value ?? "")) else { return }
            self.file(urls, into: channel, base: server.url)
        }
        return true
    }

    func file(_ urls: [URL], into channel: Int, base: URL) {
        let client = ArchiveClient(base: base)
        Task { @MainActor in
            var firstError: Error?
            for url in urls {
                do {
                    try await client.add(.file(url, source: nil, temporary: false), to: channel)
                    NSWorkspace.shared.recycle([url], completionHandler: nil)
                } catch {
                    firstError = firstError ?? error
                }
            }
            webView.reload()
            if let firstError {
                let alert = NSAlert()
                alert.messageText = "Some files were not added"
                alert.informativeText = firstError.localizedDescription
                alert.beginSheetModal(for: window, completionHandler: nil)
            }
        }
    }

    func updateTitle() {
        let page = webView.title?.replacingOccurrences(of: " · CHANNEL", with: "") ?? ""
        window.title = (page.isEmpty ? appName : page) + (readOnly ? " — Read-Only" : "")
    }

    func showArchive(channel: Int?) {
        if let channel, let server, window.contentView === webView {
            webView.load(URLRequest(url: server.url.appendingPathComponent("channel/\(channel)")))
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Reloads the page after the menu bar tray added to a channel it shows.
    func archiveChanged(channel: Int) {
        guard window.isVisible, let path = webView.url?.path else { return }
        if path == "/" || path == "/channel/\(channel)" { webView.reload() }
    }

    func showAlert(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.beginSheetModal(for: window)
    }

    func fail(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "Quit")
        alert.runModal()
        releaseLockIfOurs()
        NSApp.terminate(nil)
    }

    func releaseLockIfOurs() {
        if !readOnly { work.sync { releaseLock() } }
    }

    // MARK: Web view

    func isLocal(_ url: URL?) -> Bool {
        guard let url, let server else { return false }
        return url.host == "127.0.0.1" && url.port == server.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if isLocal(url) || url.scheme == "about" {
            decisionHandler(action.shouldPerformDownload ? .download : .allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(response.canShowMIMEType ? .allow : .download)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let downloads = fm.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        let base = (suggestedFilename as NSString).deletingPathExtension
        let ext = (suggestedFilename as NSString).pathExtension
        var target = downloads.appendingPathComponent(suggestedFilename)
        var counter = 2
        while fm.fileExists(atPath: target.path) {
            target = downloads.appendingPathComponent(ext.isEmpty ? "\(base) \(counter)" : "\(base) \(counter).\(ext)")
            counter += 1
        }
        completionHandler(target)
    }

    func downloadDidFinish(_ download: WKDownload) {
        NSSound(named: "Glass")?.play()
    }

    // target=_blank links open in the default browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    // MARK: Menu actions

    @objc func reload() { webView.reload() }
    @objc func goBack() { webView.goBack() }
    @objc func goForward() { webView.goForward() }
    @objc func goHome() { if let server { webView.load(URLRequest(url: server.url)) } }
    @objc func zoomIn() { webView.pageZoom = min(webView.pageZoom + 0.1, 3) }
    @objc func zoomOut() { webView.pageZoom = max(webView.pageZoom - 0.1, 0.5) }
    @objc func actualSize() { webView.pageZoom = 1 }
    @objc func showInFinder() {
        let database = useICloud ? Paths.cloudDB : localFolder?.appendingPathComponent("archive.db")
        if let database { NSWorkspace.shared.activateFileViewerSelecting([database]) }
    }

    func makeMenu() -> NSMenu {
        func item(_ title: String, _ action: Selector?, _ key: String = "", _ modifiers: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = modifiers
            return item
        }
        func submenu(_ title: String, _ items: [NSMenuItem?]) -> NSMenuItem {
            let menu = NSMenu(title: title)
            items.compactMap { $0 }.forEach(menu.addItem)
            let holder = NSMenuItem()
            holder.submenu = menu
            return holder
        }
        let main = NSMenu()
        main.addItem(submenu(appName, [
            item("About \(appName)", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            item("Update \(appName)…", #selector(confirmUpdate)),
            .separator(),
            item("Hide \(appName)", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            .separator(),
            item("Quit \(appName)", #selector(NSApplication.terminate(_:)), "q"),
        ]))
        main.addItem(submenu("File", [
            item("Show Archive in Finder", #selector(showInFinder)),
            useICloud ? nil : item("Choose Archive Folder…", #selector(chooseLocalFolder), "o"),
            useICloud ? nil : item("Move Archive to iCloud Drive…", #selector(moveToICloud)),
            .separator(),
            item("Close Window", #selector(NSWindow.performClose(_:)), "w"),
        ]))
        main.addItem(submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ]))
        main.addItem(submenu("View", [
            item("Reload", #selector(reload), "r"),
            .separator(),
            item("Actual Size", #selector(actualSize), "0"),
            item("Zoom In", #selector(zoomIn), "+"),
            item("Zoom Out", #selector(zoomOut), "-"),
            .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ]))
        main.addItem(submenu("Go", [
            item("Back", #selector(goBack), "["),
            item("Forward", #selector(goForward), "]"),
            item("All Channels", #selector(goHome), "h", [.command, .shift]),
        ]))
        let windowMenu = submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:))),
        ])
        NSApp.windowsMenu = windowMenu.submenu
        main.addItem(windowMenu)
        return main
    }
}

// MARK: - Icon (used by build.sh: `ArenaArchive --make-icon <dir.iconset>`)

func makeIconSet(at directory: URL) throws {
    try fm.createDirectory(at: directory, withIntermediateDirectories: true)
    for size in [16, 32, 128, 256, 512] {
        for scale in [1, 2] {
            let pixels = size * scale
            let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                                       samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                       bytesPerRow: 0, bitsPerPixel: 0)!
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            let unit = CGFloat(pixels) / 1024
            let tile = NSRect(x: 100 * unit, y: 100 * unit, width: 824 * unit, height: 824 * unit)
            NSColor(red: 0x11 / 255, green: 0x11 / 255, blue: 0x11 / 255, alpha: 1).setFill()
            NSBezierPath(roundedRect: tile, xRadius: 185 * unit, yRadius: 185 * unit).fill()
            // A silver "C": the glyph outline filled with a brushed-metal gradient.
            let font = NSFont.systemFont(ofSize: 640 * unit, weight: .heavy)
            var glyph = CGGlyph(0)
            CTFontGetGlyphsForCharacters(font, Array("C".utf16), &glyph, 1)
            let mark = NSBezierPath()
            mark.move(to: .zero)
            mark.append(withCGGlyph: glyph, in: font)
            let bounds = mark.bounds
            mark.transform(using: AffineTransform(translationByX: tile.midX - bounds.midX, byY: tile.midY - bounds.midY))
            let silver = NSGradient(colorsAndLocations:
                (NSColor(white: 0.97, alpha: 1), 0), (NSColor(white: 0.80, alpha: 1), 0.45),
                (NSColor(white: 0.62, alpha: 1), 0.55), (NSColor(white: 0.88, alpha: 1), 1))!
            silver.draw(in: mark, angle: -90)
            NSGraphicsContext.restoreGraphicsState()
            let name = scale == 1 ? "icon_\(size)x\(size).png" : "icon_\(size)x\(size)@2x.png"
            try rep.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(name))
        }
    }
}

// MARK: - Main

@main
enum Main {
    static func main() throws {
        let arguments = CommandLine.arguments
        if arguments.count == 3 && arguments[1] == "--make-icon" {
            try makeIconSet(at: URL(fileURLWithPath: arguments[2]))
            exit(0)
        }
        // Prints what the update button would show, e.g. for troubleshooting.
        if arguments.count == 2 && arguments[1] == "--check-updates" {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            print(String(decoding: try encoder.encode(checkForUpdates()), as: UTF8.self))
            exit(0)
        }

        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}

// MARK: - Web view

/// A web view that takes file drops itself, so the app knows where the files came
/// from and can move the originals instead of only copying their contents.
final class ArchiveWebView: WKWebView {
    var onFiles: (([URL], NSPoint) -> Bool)?

    private func droppedFiles(_ sender: NSDraggingInfo) -> [URL] {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        let urls = sender.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: options) as? [URL] ?? []
        return urls.filter { url in
            var isFolder: ObjCBool = false
            return fm.fileExists(atPath: url.path, isDirectory: &isFolder) && !isFolder.boolValue
        }
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        droppedFiles(sender).isEmpty ? super.draggingEntered(sender) : .copy
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        droppedFiles(sender).isEmpty ? super.draggingUpdated(sender) : .copy
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let urls = droppedFiles(sender)
        guard !urls.isEmpty else { return super.performDragOperation(sender) }
        let point = convert(sender.draggingLocation, from: nil)
        return onFiles?(urls, point) ?? super.performDragOperation(sender)
    }
}
