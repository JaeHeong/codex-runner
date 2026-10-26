/* GUI-subsystem supervisor: no console is allocated, even briefly at logon.
 * Keep the scheduled task attached to its worker and propagate its exit code.
 * No shell, elevation, detached process or native Codex shutdown is involved.
 */
#include <windows.h>
#include <wchar.h>
#include <stdlib.h>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show)
{
    wchar_t executable[32768];
    DWORD length, result, wait_result;
    wchar_t *separator, *command;
    size_t count;
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    SECURITY_ATTRIBUTES security = {sizeof(SECURITY_ATTRIBUTES), NULL, TRUE};
    HANDLE null_stream;
    (void)instance; (void)previous; (void)show;
    length = GetModuleFileNameW(NULL, executable, 32768);
    if (!length || length >= 32768) return ERROR_INSUFFICIENT_BUFFER;
    separator = wcsrchr(executable, L'\\');
    if (!separator) return ERROR_INVALID_NAME;
    if (wcscpy_s(separator + 1, 32768 - (size_t)(separator + 1 - executable), L"codex-runner.exe"))
        return ERROR_INSUFFICIENT_BUFFER;
    count = wcslen(executable) + wcslen(arguments) + 4;
    if (count > 32767) return ERROR_INSUFFICIENT_BUFFER;
    command = (wchar_t *)calloc(count, sizeof(wchar_t));
    if (!command) return ERROR_NOT_ENOUGH_MEMORY;
    if (swprintf_s(command, count, L"\"%ls\" %ls", executable, arguments) < 0) {
        free(command); return ERROR_INVALID_PARAMETER;
    }
    null_stream = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, &security,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (null_stream == INVALID_HANDLE_VALUE) { result = GetLastError(); free(command); return (int)result; }
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = startup.hStdOutput = startup.hStdError = null_stream;
    if (!CreateProcessW(executable, command, NULL, NULL, TRUE, CREATE_NO_WINDOW,
                        NULL, NULL, &startup, &process)) {
        result = GetLastError(); CloseHandle(null_stream); free(command); return (int)result;
    }
    CloseHandle(null_stream);
    free(command);
    CloseHandle(process.hThread);
    wait_result = WaitForSingleObject(process.hProcess, INFINITE);
    result = ERROR_GEN_FAILURE;
    if (wait_result == WAIT_OBJECT_0) GetExitCodeProcess(process.hProcess, &result);
    CloseHandle(process.hProcess);
    return (int)result;
}
