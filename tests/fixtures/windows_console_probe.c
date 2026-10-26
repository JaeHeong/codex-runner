/* Test-owned console executable; never uses a user account or Codex runtime. */
#include <windows.h>
#include <stdio.h>

int wmain(int argc, wchar_t **argv)
{
    FILE *out;
    int i;
    if (argc < 2 || _wfopen_s(&out, argv[1], L"w, ccs=UTF-8")) return 2;
    fwprintf(out, L"console=%d\n", GetConsoleWindow() != NULL);
    for (i = 2; i < argc; ++i) fwprintf(out, L"%ls\n", argv[i]);
    fclose(out);
    Sleep(1200);
    return 37;
}
