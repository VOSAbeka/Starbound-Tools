using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class WindowsLauncher
{
    [STAThread]
    private static void Main()
    {
        string appDirectory = AppDomain.CurrentDomain.BaseDirectory;
        string executableName = Path.GetFileNameWithoutExtension(
            Process.GetCurrentProcess().MainModule.FileName
        );
        string scriptName = executableName.StartsWith("03_")
            ? "regenerate_biome_gui.py"
            : executableName.StartsWith("02_")
                ? "json_to_world_gui.py"
                : "world_to_json_gui.py";
        string packagedScriptDirectory = Path.Combine(appDirectory, "scipt");
        string scriptPath = Path.Combine(
            Directory.Exists(packagedScriptDirectory) ? packagedScriptDirectory : appDirectory,
            scriptName
        );
        if (!File.Exists(scriptPath))
        {
            MessageBox.Show(
                "The program file could not be found:\n" + scriptPath,
                "Starbound World Editor",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return;
        }

        string configuredPython = Environment.GetEnvironmentVariable("PYTHONW_EXE");
        string[] candidates = new string[]
        {
            configuredPython,
            "pythonw.exe",
            @"D:\Softwares\Python\pythonw.exe",
            "pyw.exe"
        };
        foreach (string candidate in candidates)
        {
            if (String.IsNullOrWhiteSpace(candidate))
                continue;
            try
            {
                string arguments = candidate.EndsWith("pyw.exe", StringComparison.OrdinalIgnoreCase)
                    ? "-3 \"" + scriptPath + "\""
                    : "\"" + scriptPath + "\"";
                ProcessStartInfo startInfo = new ProcessStartInfo
                {
                    FileName = candidate,
                    Arguments = arguments,
                    WorkingDirectory = appDirectory,
                    UseShellExecute = false,
                    CreateNoWindow = true
                };
                Process.Start(startInfo);
                return;
            }
            catch
            {
            }
        }

        MessageBox.Show(
            "No usable Python 3 graphical runtime was found.\n\n" +
            "Set the PYTHONW_EXE environment variable to pythonw.exe, " +
            "or double-click the .cmd launcher in this folder.",
            "Starbound World Editor",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error
        );
    }
}
