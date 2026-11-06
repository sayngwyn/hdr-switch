// HDR Switch: turns Windows HDR on when the game starts and back off when it ends, as a plain SKSE / F4SE plugin. One DLL for Skyrim SE (1.5.97 to 1.7.x) and
// Fallout 4 (1.10.163 to 1.11.x): it reads no game memory, so it has no game version to match. Plain C++ and the Windows SDK; it needs nothing else.
//
// Why a plugin: the loader runs it before the game opens its window, so the game and its HDR mods find HDR already on when they look. A program that watches for
// the game starting is too late for those, and it reacts to every copy of the game on the PC, test copies included.
//
// What it does at load:
//   - nothing when HDR_SWITCH_OFF is set in the environment (test runners set it), when the game is not on the normal desktop (a hidden test game), when the ini
//     says bEnabled=0, when the primary monitor cannot do HDR, or when HDR is already on (then it is not ours to turn off);
//   - otherwise it starts a small watcher, then turns HDR on for the primary monitor. The watcher is rundll32 running this same DLL's HdrSwitchWatch, which waits
//     for the game process to end, however it ends (a crash, End Task, a kill of the whole process tree), and turns HDR off again. MO2 sees the watcher end a moment
//     after the game.
//
// Windows 11 24H2 and later have their own HDR calls (types 15 and 16), which tell HDR from Auto Color Management, where the older "advanced color" state also
// reads on; older Windows answers the old ones (9 and 10). Structures as in the Windows SDK's wingdi.h (10.0.26100), declared here so any SDK builds it.
//
// Log: Documents\My Games\<game>\<SKSE or F4SE>\HdrSwitch.log. Settings: HdrSwitch.ini beside the DLL (optional): [General] bEnabled=1.
#include <windows.h>

#include <shlobj.h>

#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "version.h"
#include "xse.h"

namespace
{
	constexpr char     kName[] = "HdrSwitch";
	constexpr wchar_t  kOffVariable[] = L"HDR_SWITCH_OFF";
	constexpr DWORD    kSettleMs = 5000;   // Windows takes a moment to change modes; how long to look for the new state
	constexpr DWORD    kSpawnMs = 10000;   // how long the game waits for the helper that starts the watcher

	enum : std::uint32_t
	{
		kGetAdvancedColorInfo = 9,
		kSetAdvancedColorState = 10,
		kGetAdvancedColorInfo2 = 15,
		kSetHdrState = 16,
	};

	struct GetColorInfo
	{
		DISPLAYCONFIG_DEVICE_INFO_HEADER header;
		std::uint32_t                    value;   // bit 0 supported, bit 1 enabled
		std::uint32_t                    colorEncoding;
		std::uint32_t                    bitsPerColorChannel;
	};

	struct GetColorInfo2
	{
		DISPLAYCONFIG_DEVICE_INFO_HEADER header;
		std::uint32_t                    value;   // bit 4 HDR supported, bit 5 HDR on by the user's setting
		std::uint32_t                    colorEncoding;
		std::uint32_t                    bitsPerColorChannel;
		std::uint32_t                    activeColorMode;
	};

	struct SetState   // the old call's and the new call's are the same shape: the header and one flag in bit 0
	{
		DISPLAYCONFIG_DEVICE_INFO_HEADER header;
		std::uint32_t                    value;
	};

	struct Monitor
	{
		LUID          adapter{};
		std::uint32_t target{ 0 };
	};

	struct HdrState
	{
		bool known{ false };
		bool supported{ false };
		bool on{ false };
		bool modern{ false };   // answered by the 24H2 call
	};

	wchar_t g_log[MAX_PATH * 2] = {};

	void log_line(const char* a_format, ...)
	{
		if (!g_log[0]) {
			return;
		}
		FILE* file = nullptr;
		if (_wfopen_s(&file, g_log, L"a") != 0 || !file) {
			return;
		}
		SYSTEMTIME now;
		GetLocalTime(&now);
		std::fprintf(file, "[%02u:%02u:%02u] ", now.wHour, now.wMinute, now.wSecond);
		va_list args;
		va_start(args, a_format);
		std::vfprintf(file, a_format, args);
		va_end(args);
		std::fputc('\n', file);
		std::fclose(file);
	}

	// Documents\My Games\<game>\<loader>\HdrSwitch.log. a_fresh: start the file over (a new game); the watcher adds to it.
	void open_log(const char* a_loader, bool a_fresh)
	{
		PWSTR documents = nullptr;
		if (FAILED(SHGetKnownFolderPath(FOLDERID_Documents, 0, nullptr, &documents)) || !documents) {
			return;
		}
		const bool   skse = std::strcmp(a_loader, "SKSE") == 0;
		std::wstring path(documents);
		CoTaskMemFree(documents);
		path += L"\\My Games";
		CreateDirectoryW(path.c_str(), nullptr);
		path += skse ? L"\\Skyrim Special Edition" : L"\\Fallout4";
		CreateDirectoryW(path.c_str(), nullptr);
		path += skse ? L"\\SKSE" : L"\\F4SE";
		CreateDirectoryW(path.c_str(), nullptr);
		path += L"\\HdrSwitch.log";
		if (path.size() >= std::size(g_log)) {
			return;
		}
		wcscpy_s(g_log, path.c_str());
		if (a_fresh) {
			FILE* file = nullptr;
			if (_wfopen_s(&file, g_log, L"w") == 0 && file) {
				std::fclose(file);
			}
		}
	}

	// The primary monitor: the active display whose desktop starts at 0,0.
	bool primary_monitor(Monitor& a_out)
	{
		std::vector<DISPLAYCONFIG_PATH_INFO> paths;
		std::vector<DISPLAYCONFIG_MODE_INFO> modes;
		LONG                                 result = ERROR_INSUFFICIENT_BUFFER;
		for (int tries = 0; tries < 5 && result == ERROR_INSUFFICIENT_BUFFER; ++tries) {
			UINT32 pathCount = 0;
			UINT32 modeCount = 0;
			if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &pathCount, &modeCount) != ERROR_SUCCESS) {
				return false;
			}
			paths.resize(pathCount);
			modes.resize(modeCount);
			result = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &pathCount, paths.data(), &modeCount, modes.data(), nullptr);
			paths.resize(pathCount);
			modes.resize(modeCount);
		}
		if (result != ERROR_SUCCESS) {
			return false;
		}
		for (const auto& path : paths) {
			const auto index = path.sourceInfo.modeInfoIdx;
			if (index == DISPLAYCONFIG_PATH_MODE_IDX_INVALID || index >= modes.size() || modes[index].infoType != DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE) {
				continue;
			}
			const auto& position = modes[index].sourceMode.position;
			if (position.x == 0 && position.y == 0) {
				a_out.adapter = path.targetInfo.adapterId;
				a_out.target = path.targetInfo.id;
				return true;
			}
		}
		return false;
	}

	template <class T>
	void fill_header(T& a_info, std::uint32_t a_type, const Monitor& a_monitor)
	{
		a_info.header.type = static_cast<DISPLAYCONFIG_DEVICE_INFO_TYPE>(a_type);
		a_info.header.size = sizeof(T);
		a_info.header.adapterId = a_monitor.adapter;
		a_info.header.id = a_monitor.target;
	}

	HdrState read_state(const Monitor& a_monitor)
	{
		HdrState      state;
		GetColorInfo2 modern{};
		fill_header(modern, kGetAdvancedColorInfo2, a_monitor);
		if (DisplayConfigGetDeviceInfo(&modern.header) == ERROR_SUCCESS) {
			state.known = state.modern = true;
			state.supported = (modern.value & (1u << 4)) != 0;
			state.on = (modern.value & (1u << 5)) != 0;
			return state;
		}
		GetColorInfo older{};
		fill_header(older, kGetAdvancedColorInfo, a_monitor);
		if (DisplayConfigGetDeviceInfo(&older.header) == ERROR_SUCCESS) {
			state.known = true;
			state.supported = (older.value & 1u) != 0;
			state.on = (older.value & 2u) != 0;
		}
		return state;
	}

	// Ask Windows for a_on and look until the state says so. False when it never did.
	bool set_hdr(const Monitor& a_monitor, bool a_on)
	{
		const auto state = read_state(a_monitor);
		SetState   request{};
		fill_header(request, state.modern ? kSetHdrState : kSetAdvancedColorState, a_monitor);
		request.value = a_on ? 1u : 0u;
		const LONG answer = DisplayConfigSetDeviceInfo(&request.header);
		if (answer != ERROR_SUCCESS) {
			log_line("Windows refused the change: error %ld", answer);
			return false;
		}
		const auto started = GetTickCount64();
		while (GetTickCount64() - started < kSettleMs) {
			const auto now = read_state(a_monitor);
			if (now.known && now.on == a_on) {
				return true;
			}
			Sleep(100);
		}
		return false;
	}

	bool on_normal_desktop()
	{
		wchar_t name[256] = {};
		DWORD   needed = 0;
		HDESK   desktop = GetThreadDesktop(GetCurrentThreadId());
		if (!desktop || !GetUserObjectInformationW(desktop, UOI_NAME, name, sizeof(name) - sizeof(wchar_t), &needed)) {
			return true;
		}
		return _wcsicmp(name, L"Default") == 0;
	}

	// [General] bEnabled in HdrSwitch.ini beside the DLL; on when there is no file.
	bool enabled_in_ini(const std::wstring& a_dll)
	{
		const auto slash = a_dll.find_last_of(L"\\/");
		const auto ini = (slash == std::wstring::npos ? std::wstring() : a_dll.substr(0, slash + 1)) + L"HdrSwitch.ini";
		return GetPrivateProfileIntW(L"General", L"bEnabled", 1, ini.c_str()) != 0;
	}

	std::wstring module_path()
	{
		HMODULE self = nullptr;
		GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, reinterpret_cast<LPCWSTR>(&module_path), &self);
		wchar_t path[MAX_PATH * 2] = {};
		GetModuleFileNameW(self, path, static_cast<DWORD>(std::size(path)));
		return path;
	}

	// rundll32 "<this DLL>",<a_entry> <a_arguments>
	bool run_helper(const std::wstring& a_dll, const wchar_t* a_entry, const std::wstring& a_arguments, PROCESS_INFORMATION& a_process)
	{
		wchar_t system[MAX_PATH] = {};
		if (!GetSystemDirectoryW(system, MAX_PATH)) {
			return false;
		}
		const std::wstring exe = std::wstring(system) + L"\\rundll32.exe";
		std::wstring       command = L"\"" + exe + L"\" \"" + a_dll + L"\"," + a_entry + L" " + a_arguments;
		STARTUPINFOW       startup{};
		startup.cb = sizeof(startup);
		return CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, nullptr, &startup, &a_process) != FALSE;
	}

	// The game starts a short-lived helper (HdrSwitchSpawn), which starts the watcher and exits. So the watcher's parent is already gone and it isn't part of the
	// game's process tree: ending the whole tree ends the game alone, and the watcher is still there to turn HDR off.
	// Arguments for both: <game pid> <adapter low> <adapter high> <target> <loader>
	bool start_watcher(const std::wstring& a_dll, const Monitor& a_monitor, const char* a_loader)
	{
		wchar_t arguments[64];
		swprintf_s(arguments, L"%lu %lu %ld %u %hs", GetCurrentProcessId(), a_monitor.adapter.LowPart, a_monitor.adapter.HighPart, a_monitor.target, a_loader);
		PROCESS_INFORMATION process{};
		if (!run_helper(a_dll, L"HdrSwitchSpawn", arguments, process)) {
			log_line("could not start the watcher: error %lu", GetLastError());
			return false;
		}
		DWORD      code = 1;
		const bool ended = WaitForSingleObject(process.hProcess, kSpawnMs) == WAIT_OBJECT_0;
		if (ended) {
			GetExitCodeProcess(process.hProcess, &code);
		} else {
			TerminateProcess(process.hProcess, 1);
		}
		CloseHandle(process.hThread);
		CloseHandle(process.hProcess);
		if (!ended || code != 0) {
			log_line("could not start the watcher: helper %s, code %lu", ended ? "failed" : "timed out", code);
			return false;
		}
		return true;
	}

	void on_load(const char* a_loader)
	{
		static bool done = false;
		if (done) {
			return;
		}
		done = true;
		open_log(a_loader, true);
		log_line("%s %s, %s", kName, HDRSWITCH_VERSION, a_loader);
		wchar_t off[8] = {};
		if (GetEnvironmentVariableW(kOffVariable, off, static_cast<DWORD>(std::size(off))) > 0) {
			log_line("skipped: %ls is set (a test run)", kOffVariable);
			return;
		}
		if (!on_normal_desktop()) {
			log_line("skipped: the game is on a hidden desktop (a test run)");
			return;
		}
		const auto dll = module_path();
		if (!enabled_in_ini(dll)) {
			log_line("skipped: bEnabled=0 in HdrSwitch.ini");
			return;
		}
		Monitor monitor;
		if (!primary_monitor(monitor)) {
			log_line("skipped: no primary monitor found");
			return;
		}
		const auto state = read_state(monitor);
		if (!state.known || !state.supported) {
			log_line("skipped: the primary monitor does not offer HDR");
			return;
		}
		if (state.on) {
			log_line("HDR is already on: leaving it alone, on and at exit");
			return;
		}
		// The watcher first: however early the game dies from here on, something is there to turn HDR back off.
		if (!start_watcher(dll, monitor, a_loader)) {
			log_line("HDR left off: nothing would turn it off at exit");
			return;
		}
		log_line(set_hdr(monitor, true) ? "HDR on" : "HDR did NOT turn on");
	}

	bool query(const xse::Interface* a_xse, xse::PluginInfo* a_info)
	{
		if (a_info) {
			a_info->infoVersion = 1;
			a_info->name = kName;
			a_info->version = 1;
		}
		return a_xse && !a_xse->isEditor;
	}

	constexpr std::uint32_t kPacked = (static_cast<std::uint32_t>(HDRSWITCH_MAJOR & 0xFF) << 24) | (static_cast<std::uint32_t>(HDRSWITCH_MINOR & 0xFF) << 16) |
	                                  (static_cast<std::uint32_t>(HDRSWITCH_PATCH & 0xFFF) << 4);
}

extern "C"
{
	__declspec(dllexport) xse::SKSEPluginVersionData SKSEPlugin_Version = {
		.dataVersion = xse::SKSEPluginVersionData::kVersion,
		.pluginVersion = kPacked,
		.pluginName = "HdrSwitch",
		.author = "sayngwyn",
		.supportEmail = "",
		.versionIndependenceEx = xse::SKSEPluginVersionData::kNoStructUse,
		.versionIndependence = xse::SKSEPluginVersionData::kSignatures | xse::SKSEPluginVersionData::kStructsPost629,
		.compatibleVersions = {},
		.xseMinimum = 0,
	};

	__declspec(dllexport) xse::F4SEPluginVersionData F4SEPlugin_Version = {
		.dataVersion = xse::F4SEPluginVersionData::kVersion,
		.pluginVersion = kPacked,
		.pluginName = "HdrSwitch",
		.author = "sayngwyn",
		.addressIndependence = xse::F4SEPluginVersionData::kSignatures,
		.structureIndependence = xse::F4SEPluginVersionData::kNoStructs,
		.compatibleVersions = {},
		.xseMinimum = 0,
		.reservedNonBreaking = 0,
		.reservedBreaking = 0,
		.reserved = {},
	};

	__declspec(dllexport) bool SKSEPlugin_Query(const xse::Interface* a_skse, xse::PluginInfo* a_info) { return query(a_skse, a_info); }

	__declspec(dllexport) bool SKSEPlugin_Load(const xse::Interface* a_skse)
	{
		if (a_skse && a_skse->isEditor) {
			return false;
		}
		on_load("SKSE");
		return true;
	}

	__declspec(dllexport) bool F4SEPlugin_Query(const xse::Interface* a_f4se, xse::PluginInfo* a_info) { return query(a_f4se, a_info); }

	__declspec(dllexport) bool F4SEPlugin_Load(const xse::Interface* a_f4se)
	{
		if (a_f4se && a_f4se->isEditor) {
			return false;
		}
		on_load("F4SE");
		return true;
	}

	// The short-lived helper: starts the watcher with the same arguments and exits, 0 when the watcher started.
	__declspec(dllexport) void CALLBACK HdrSwitchSpawn(HWND, HINSTANCE, LPSTR a_arguments, int)
	{
		wchar_t arguments[64] = {};
		PROCESS_INFORMATION process{};
		if (!a_arguments || MultiByteToWideChar(CP_ACP, 0, a_arguments, -1, arguments, static_cast<int>(std::size(arguments))) == 0 ||
			!run_helper(module_path(), L"HdrSwitchWatch", arguments, process)) {
			ExitProcess(1);
		}
		CloseHandle(process.hThread);
		CloseHandle(process.hProcess);
		ExitProcess(0);
	}

	// The watcher: rundll32 calls this with the arguments start_watcher wrote. Waits for the game to end, then turns HDR off.
	__declspec(dllexport) void CALLBACK HdrSwitchWatch(HWND, HINSTANCE, LPSTR a_arguments, int)
	{
		unsigned long pid = 0;
		unsigned long low = 0;
		long          high = 0;
		unsigned int  target = 0;
		char          loader[8] = {};
		if (!a_arguments || sscanf_s(a_arguments, "%lu %lu %ld %u %7s", &pid, &low, &high, &target, loader, static_cast<unsigned>(sizeof(loader))) != 5) {
			return;
		}
		open_log(loader, false);
		Monitor monitor;
		monitor.adapter.LowPart = low;
		monitor.adapter.HighPart = high;
		monitor.target = target;
		const HANDLE game = OpenProcess(SYNCHRONIZE, FALSE, pid);
		if (game) {
			WaitForSingleObject(game, INFINITE);
			CloseHandle(game);
		}
		const bool off = set_hdr(monitor, false);
		log_line(off ? "game ended: HDR off" : "game ended: HDR did NOT turn off");
	}
}
