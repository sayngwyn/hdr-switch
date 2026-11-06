-- HDR Switch: an SKSE / F4SE plugin that turns Windows HDR on for the game and off after it (see src/main.cpp).
-- Plain C++20 and the Windows SDK; no packages, so the build never downloads anything. Build with tools/build.py.
set_project("HdrSwitch")
set_languages("c++20")

add_rules("mode.release")

target("HdrSwitch")
    set_kind("shared")
    add_files("src/*.cpp")
    add_headerfiles("src/*.h")
    add_includedirs("src")
    add_defines("UNICODE", "_UNICODE", "WIN32_LEAN_AND_MEAN", "NOMINMAX")
    add_syslinks("user32", "kernel32", "shell32", "ole32")
    set_runtimes("MT")                      -- static CRT: no Visual C++ runtime needed beside the game
    add_cxflags("/W4", "/permissive-", "/utf-8", "/Brepro", {tools = "cl"})
    add_ldflags("/Brepro", {tools = "link"})
    set_symbols("none")
