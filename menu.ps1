<#
.SYNOPSIS
    Interactive Launcher & Workflow Menu for Comic Translation & Inpainting Pipeline.
.DESCRIPTION
    Runs from anywhere on the system. Uses native Windows GUI pickers for file/folder selection.
    Orchestrates llama-translate.py, edit_comics.py, and edit_comics_comfy.py with uv or python.
#>

# Enable UTF-8 console output for beautiful unicode characters
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ==============================================================================
# RESOLVE PACKAGE DIRECTORY (DYNAMICALLY DETECTED - NO HARDCODED PATHS)
# ==============================================================================
$ScriptDir = if ($PSScriptRoot) {
    $PSScriptRoot
} elseif ($MyInvocation.MyCommand.Path) {
    Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $PWD.Path
}

$ScriptDir = (Resolve-Path $ScriptDir).Path

if (-not (Test-Path (Join-Path $ScriptDir "common.py"))) {
    Write-Host "`n  [ERROR] common.py not found in '$ScriptDir'." -ForegroundColor Red
    Write-Host "  Please ensure menu.ps1 is executed from or located within the project repository.`n" -ForegroundColor Yellow
}

$StateFile = Join-Path $ScriptDir ".menu_state.json"

# ==============================================================================
# STATE MANAGEMENT
# ==============================================================================
function Get-SavedState {
    if (Test-Path $StateFile) {
        try {
            $json = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
            return $json
        } catch {}
    }
    return [PSCustomObject]@{
        LastComicFolder    = ""
        LastWorkflowFile   = (Join-Path $ScriptDir "qwen21-gguf.json")
        LastLanguage       = "chinese"
        LastThinkingEffort = "low"
        LastDelay          = 60
    }
}

function Save-SavedState {
    param($State)
    try {
        $State | ConvertTo-Json -Depth 4 | Set-Content -Path $StateFile -Encoding UTF8
    } catch {}
}

$Global:State = Get-SavedState

# ==============================================================================
# NATIVE WINDOWS PICKERS (STA SAFE & TOPMOST)
# ==============================================================================
function Invoke-InSTA {
    param([scriptblock]$ScriptBlock)
    if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -eq 'STA') {
        return & $ScriptBlock
    }
    # MTA fallback: run in a dedicated STA runspace
    $runspace = [runspacefactory]::CreateRunspace()
    $runspace.ApartmentState = [System.Threading.ApartmentState]::STA
    $runspace.ThreadOptions = [System.Management.Automation.Runspaces.PSThreadOptions]::ReuseThread
    $runspace.Open()
    $ps = [powershell]::Create()
    $ps.Runspace = $runspace
    $null = $ps.AddScript($ScriptBlock)
    $results = $ps.Invoke()
    $runspace.Close()
    $runspace.Dispose()
    if ($results -and $results.Count -eq 1) { return $results[0] }
    return $results
}

function Select-WindowsFolder {
    param(
        [string]$Title = "Select Comic Folder",
        [string]$InitialFolder = ""
    )
    Write-Host "`n  [i] Opening Windows Folder Picker dialog..." -ForegroundColor Cyan

    $selected = Invoke-InSTA {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = $Title
        $dialog.ShowNewFolderButton = $true

        $prop = $dialog.GetType().GetProperty('AutoUpgradeEnabled')
        if ($prop) { $dialog.AutoUpgradeEnabled = $true }

        if ($InitialFolder -and (Test-Path $InitialFolder)) {
            $dialog.SelectedPath = $InitialFolder
        }

        # Use an invisible TopMost owner form so the picker appears in the foreground
        $owner = New-Object System.Windows.Forms.Form
        $owner.TopMost = $true
        $owner.ShowInTaskbar = $false
        $owner.WindowState = [System.Windows.Forms.FormWindowState]::Minimized
        $owner.Show()
        $owner.Visible = $false

        $result = $dialog.ShowDialog($owner)
        $owner.Dispose()

        if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
            return $dialog.SelectedPath
        }
        return $null
    }

    if ($selected) {
        Write-Host "  [✓] Selected folder: $selected" -ForegroundColor Green
        $Global:State.LastComicFolder = $selected
        Save-SavedState $Global:State
        return $selected
    } else {
        Write-Host "  [!] Folder selection cancelled." -ForegroundColor Yellow
        return $null
    }
}

function Select-WindowsFile {
    param(
        [string]$Title = "Select File",
        [string]$Filter = "JSON Files (*.json)|*.json|All Files (*.*)|*.*",
        [string]$InitialPath = ""
    )
    Write-Host "`n  [i] Opening Windows File Picker dialog..." -ForegroundColor Cyan

    $selected = Invoke-InSTA {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = $Title
        $dialog.Filter = $Filter
        $dialog.RestoreDirectory = $true
        $dialog.Multiselect = $false

        if ($InitialPath -and (Test-Path $InitialPath)) {
            if (Test-Path $InitialPath -PathType Container) {
                $dialog.InitialDirectory = $InitialPath
            } else {
                $dialog.InitialDirectory = Split-Path $InitialPath -Parent
                $dialog.FileName = Split-Path $InitialPath -Leaf
            }
        }

        # Use an invisible TopMost owner form
        $owner = New-Object System.Windows.Forms.Form
        $owner.TopMost = $true
        $owner.ShowInTaskbar = $false
        $owner.WindowState = [System.Windows.Forms.FormWindowState]::Minimized
        $owner.Show()
        $owner.Visible = $false

        $result = $dialog.ShowDialog($owner)
        $owner.Dispose()

        if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
            return $dialog.FileName
        }
        return $null
    }

    if ($selected) {
        Write-Host "  [✓] Selected file: $selected" -ForegroundColor Green
        return $selected
    } else {
        Write-Host "  [!] File selection cancelled." -ForegroundColor Yellow
        return $null
    }
}

# ==============================================================================
# SCRIPT EXECUTION ENGINE
# ==============================================================================
function Invoke-PipelineScript {
    param(
        [string]$ScriptName,
        [string[]]$Arguments
    )

    $targetScript = Join-Path $ScriptDir $ScriptName
    if (-not (Test-Path $targetScript)) {
        Write-Host "`n  [ERROR] Script not found: $targetScript" -ForegroundColor Red
        return $false
    }

    $uvExe = Get-Command "uv" -ErrorAction SilentlyContinue
    $venvPy = Join-Path $ScriptDir ".venv\Scripts\python.exe"

    Write-Host ""
    Write-Host "================================================================================" -ForegroundColor Cyan
    Write-Host " Executing: $ScriptName" -ForegroundColor Cyan
    Write-Host " Package  : $ScriptDir" -ForegroundColor DarkGray
    Write-Host " Command  : uv run python $ScriptName $($Arguments -join ' ')" -ForegroundColor DarkGray
    Write-Host "================================================================================" -ForegroundColor Cyan
    Write-Host ""

    $exitCode = 0
    if ($uvExe) {
        & $uvExe.Source run --project "$ScriptDir" python "$targetScript" @Arguments
        $exitCode = $LASTEXITCODE
    } elseif (Test-Path $venvPy) {
        & "$venvPy" "$targetScript" @Arguments
        $exitCode = $LASTEXITCODE
    } else {
        $sysPy = Get-Command "python" -ErrorAction SilentlyContinue
        if ($sysPy) {
            & $sysPy.Source "$targetScript" @Arguments
            $exitCode = $LASTEXITCODE
        } else {
            Write-Host "`n  [ERROR] Neither 'uv' nor 'python' was found in PATH." -ForegroundColor Red
            return $false
        }
    }

    return ($exitCode -eq 0)
}

# ==============================================================================
# HELPER PROMPTS
# ==============================================================================
function Prompt-Language {
    param([string]$DefaultLang = "chinese")

    Write-Host "`n  Source Language:" -ForegroundColor White
    Write-Host "    [1] Chinese  (zh)" -ForegroundColor Cyan
    Write-Host "    [2] Japanese (ja)" -ForegroundColor Cyan
    Write-Host "    [3] Korean   (ko)" -ForegroundColor Cyan
    Write-Host "    [4] Custom"        -ForegroundColor Cyan

    $defChoice = "1"
    if ($DefaultLang -match "ja|jp|japanese") { $defChoice = "2" }
    elseif ($DefaultLang -match "ko|kr|korean") { $defChoice = "3" }

    $ans = Read-Host "  Select language [1-4] (Default: $defChoice - $DefaultLang)"
    if ([string]::IsNullOrWhiteSpace($ans)) { $ans = $defChoice }

    switch ($ans.Trim()) {
        "1" { return "chinese" }
        "2" { return "japanese" }
        "3" { return "korean" }
        "4" {
            $custom = Read-Host "  Enter custom source language"
            if ([string]::IsNullOrWhiteSpace($custom)) { return $DefaultLang }
            return $custom.Trim()
        }
        default { return $DefaultLang }
    }
}

function Prompt-ConfirmExecution {
    param([string]$Message = "Start execution now?")
    $ans = Read-Host "`n  $Message [Y/n]"
    if ([string]::IsNullOrWhiteSpace($ans) -or $ans.Trim().ToLower() -eq "y") {
        return $true
    }
    return $false
}

function Pause-Menu {
    Write-Host ""
    Write-Host "  Press Enter to return to the main menu..." -ForegroundColor DarkGray
    $null = Read-Host
}

# ==============================================================================
# MENU ACTIONS
# ==============================================================================

# 1. Translate Comic Pages
function Action-Translate {
    Write-Host "`n--- [1] TRANSLATE COMIC PAGES (llama-translate.py) ---" -ForegroundColor Yellow
    
    $folder = Select-WindowsFolder -Title "Select Comic Folder to Translate" -InitialFolder $Global:State.LastComicFolder
    if (-not $folder) { return }

    # Quick image count check
    $images = Get-ChildItem -Path $folder -File | Where-Object { $_.Extension -match '^\.(png|jpe?g|webp)$' }
    if ($images.Count -eq 0) {
        Write-Host "  [!] Warning: No images (.png, .jpg, .webp) found in '$folder'." -ForegroundColor Yellow
        if (-not (Prompt-ConfirmExecution "Proceed anyway?")) { return }
    } else {
        Write-Host "  [i] Found $($images.Count) image page(s) in folder." -ForegroundColor Green
    }

    $lang = Prompt-Language -DefaultLang $Global:State.LastLanguage
    $Global:State.LastLanguage = $lang
    Save-SavedState $Global:State

    Write-Host "`n  Configuration Mode:" -ForegroundColor White
    Write-Host "    [1] Quick Run (Recommended: Low thinking effort, 60s delay)" -ForegroundColor Green
    Write-Host "    [2] Customize settings (Thinking effort, delay, skip errors, etc.)" -ForegroundColor Cyan
    $mode = Read-Host "  Choose mode [1/2] (Default: 1)"
    if ([string]::IsNullOrWhiteSpace($mode)) { $mode = "1" }

    $argsList = @($folder, "--lang", $lang)

    if ($mode.Trim() -eq "2") {
        Write-Host "`n  Thinking Effort Level:" -ForegroundColor White
        Write-Host "    [1] low (default)  [2] medium  [3] high  [4] none" -ForegroundColor Cyan
        $eff = Read-Host "  Select effort [1-4] (Default: 1)"
        $effortMap = @{ "1"="low"; "2"="medium"; "3"="high"; "4"="none" }
        $chosenEffort = if ($effortMap.ContainsKey($eff)) { $effortMap[$eff] } else { "low" }
        $argsList += @("--thinking-effort", $chosenEffort)

        $delayInput = Read-Host "  Delay between API calls in seconds (Default: 60)"
        if ($delayInput -and [double]::TryParse($delayInput, [ref]$null)) {
            $argsList += @("--delay", $delayInput.Trim())
        }

        $skipErr = Read-Host "  Skip earlier failed pages and resume strictly from last success? [y/N]"
        if ($skipErr -and $skipErr.Trim().ToLower() -eq "y") {
            $argsList += "--skip-errors"
        }

        $noCtx = Read-Host "  Disable passing previous page translation context? [y/N]"
        if ($noCtx -and $noCtx.Trim().ToLower() -eq "y") {
            $argsList += "--no-context"
        }
    }

    if (-not (Prompt-ConfirmExecution)) {
        Write-Host "  [!] Operation cancelled by user." -ForegroundColor Yellow
        return
    }

    $null = Invoke-PipelineScript -ScriptName "llama-translate.py" -Arguments $argsList
}

# 2. Inpaint via ComfyUI
function Action-InpaintComfy {
    Write-Host "`n--- [2] INPAINT VIA COMFYUI (edit_comics_comfy.py) ---" -ForegroundColor Yellow

    Write-Host "`n  Input Source:" -ForegroundColor White
    Write-Host "    [1] Comic Folder (auto-detects translations.json) [Default]" -ForegroundColor Cyan
    Write-Host "    [2] Specific Translations JSON file" -ForegroundColor Cyan
    $srcChoice = Read-Host "  Select [1/2] (Default: 1)"
    if ([string]::IsNullOrWhiteSpace($srcChoice)) { $srcChoice = "1" }

    $targetInput = $null
    if ($srcChoice.Trim() -eq "2") {
        $initJson = if ($Global:State.LastComicFolder) { Join-Path $Global:State.LastComicFolder "translations.json" } else { "" }
        $targetInput = Select-WindowsFile -Title "Select Translations JSON File" -Filter "Translations (*.json)|*.json|All Files (*.*)|*.*" -InitialPath $initJson
    } else {
        $targetInput = Select-WindowsFolder -Title "Select Comic Folder with translations.json" -InitialFolder $Global:State.LastComicFolder
    }

    if (-not $targetInput) { return }

    if (Test-Path $targetInput -PathType Container) {
        $transJson = Join-Path $targetInput "translations.json"
        if (-not (Test-Path $transJson)) {
            Write-Host "  [!] Warning: 'translations.json' not found in '$targetInput'!" -ForegroundColor Red
            Write-Host "      Please run Translation (Option 1) first to generate translations.json." -ForegroundColor Yellow
            if (-not (Prompt-ConfirmExecution "Proceed anyway?")) { return }
        } else {
            Write-Host "  [✓] Verified translations.json is present." -ForegroundColor Green
        }
    }

    $workflowInit = if ($Global:State.LastWorkflowFile -and (Test-Path $Global:State.LastWorkflowFile)) {
        $Global:State.LastWorkflowFile
    } else {
        (Join-Path $ScriptDir "qwen21-gguf.json")
    }

    $workflow = Select-WindowsFile -Title "Select Exported ComfyUI Workflow JSON (API format)" -Filter "JSON Files (*.json)|*.json" -InitialPath $workflowInit
    if (-not $workflow) { return }

    $Global:State.LastWorkflowFile = $workflow
    Save-SavedState $Global:State

    $lang = Prompt-Language -DefaultLang $Global:State.LastLanguage
    $Global:State.LastLanguage = $lang
    Save-SavedState $Global:State

    Write-Host "`n  Options:" -ForegroundColor White
    $overwrite = Read-Host "  Overwrite existing images in 'edited\' subfolder? [y/N]"
    $delayInput = Read-Host "  Delay between ComfyUI calls in seconds (Default: 120)"

    $argsList = @($targetInput, $workflow, "--lang", $lang)
    if ($overwrite -and $overwrite.Trim().ToLower() -eq "y") {
        $argsList += "--overwrite"
    }
    if ($delayInput -and [double]::TryParse($delayInput, [ref]$null)) {
        $argsList += @("--delay", $delayInput.Trim())
    }

    if (-not (Prompt-ConfirmExecution)) {
        Write-Host "  [!] Operation cancelled by user." -ForegroundColor Yellow
        return
    }

    $null = Invoke-PipelineScript -ScriptName "edit_comics_comfy.py" -Arguments $argsList
}

# 3. Inpaint via SD Server
function Action-InpaintSD {
    Write-Host "`n--- [3] INPAINT VIA SD SERVER (edit_comics.py) ---" -ForegroundColor Yellow

    Write-Host "`n  Input Source:" -ForegroundColor White
    Write-Host "    [1] Comic Folder (auto-detects translations.json) [Default]" -ForegroundColor Cyan
    Write-Host "    [2] Specific Translations JSON file" -ForegroundColor Cyan
    $srcChoice = Read-Host "  Select [1/2] (Default: 1)"
    if ([string]::IsNullOrWhiteSpace($srcChoice)) { $srcChoice = "1" }

    $targetInput = $null
    if ($srcChoice.Trim() -eq "2") {
        $initJson = if ($Global:State.LastComicFolder) { Join-Path $Global:State.LastComicFolder "translations.json" } else { "" }
        $targetInput = Select-WindowsFile -Title "Select Translations JSON File" -Filter "Translations (*.json)|*.json|All Files (*.*)|*.*" -InitialPath $initJson
    } else {
        $targetInput = Select-WindowsFolder -Title "Select Comic Folder with translations.json" -InitialFolder $Global:State.LastComicFolder
    }

    if (-not $targetInput) { return }

    if (Test-Path $targetInput -PathType Container) {
        $transJson = Join-Path $targetInput "translations.json"
        if (-not (Test-Path $transJson)) {
            Write-Host "  [!] Warning: 'translations.json' not found in '$targetInput'!" -ForegroundColor Red
            Write-Host "      Please run Translation (Option 1) first to generate translations.json." -ForegroundColor Yellow
            if (-not (Prompt-ConfirmExecution "Proceed anyway?")) { return }
        } else {
            Write-Host "  [✓] Verified translations.json is present." -ForegroundColor Green
        }
    }

    $lang = Prompt-Language -DefaultLang $Global:State.LastLanguage
    $Global:State.LastLanguage = $lang
    Save-SavedState $Global:State

    Write-Host "`n  Configuration Mode:" -ForegroundColor White
    Write-Host "    [1] Quick Run (Steps: 4, CFG: 1.0, Max Side: 1280px, Delay: 60s)" -ForegroundColor Green
    Write-Host "    [2] Customize parameters" -ForegroundColor Cyan
    $mode = Read-Host "  Choose mode [1/2] (Default: 1)"
    if ([string]::IsNullOrWhiteSpace($mode)) { $mode = "1" }

    $argsList = @($targetInput, "--lang", $lang)

    if ($mode.Trim() -eq "2") {
        $steps = Read-Host "  Sampling steps (Default: 4 for Turbo)"
        if ($steps -and [int]::TryParse($steps, [ref]$null)) { $argsList += @("--steps", $steps.Trim()) }

        $cfg = Read-Host "  CFG scale (Default: 1.0 for Turbo)"
        if ($cfg -and [double]::TryParse($cfg, [ref]$null)) { $argsList += @("--cfg", $cfg.Trim()) }

        $maxSide = Read-Host "  Max image side in pixels (Default: 1280)"
        if ($maxSide -and [int]::TryParse($maxSide, [ref]$null)) { $argsList += @("--max-side", $maxSide.Trim()) }

        $delay = Read-Host "  Delay between edits in seconds (Default: 60)"
        if ($delay -and [double]::TryParse($delay, [ref]$null)) { $argsList += @("--delay", $delay.Trim()) }

        $overwrite = Read-Host "  Overwrite existing images in 'edited\'? [y/N]"
        if ($overwrite -and $overwrite.Trim().ToLower() -eq "y") { $argsList += "--overwrite" }
    }

    if (-not (Prompt-ConfirmExecution)) {
        Write-Host "  [!] Operation cancelled by user." -ForegroundColor Yellow
        return
    }

    $null = Invoke-PipelineScript -ScriptName "edit_comics.py" -Arguments $argsList
}

# 4. End-to-End Pipeline: Translate + ComfyUI
function Action-PipelineComfy {
    Write-Host "`n--- [4] END-TO-END PIPELINE: TRANSLATE + COMFYUI ---" -ForegroundColor Yellow

    $folder = Select-WindowsFolder -Title "Select Comic Folder to Process" -InitialFolder $Global:State.LastComicFolder
    if (-not $folder) { return }

    $workflowInit = if ($Global:State.LastWorkflowFile -and (Test-Path $Global:State.LastWorkflowFile)) {
        $Global:State.LastWorkflowFile
    } else {
        (Join-Path $ScriptDir "qwen21-gguf.json")
    }

    $workflow = Select-WindowsFile -Title "Select ComfyUI Workflow JSON (API format)" -Filter "JSON Files (*.json)|*.json" -InitialPath $workflowInit
    if (-not $workflow) { return }

    $Global:State.LastWorkflowFile = $workflow
    Save-SavedState $Global:State

    $lang = Prompt-Language -DefaultLang $Global:State.LastLanguage
    $Global:State.LastLanguage = $lang
    Save-SavedState $Global:State

    Write-Host "`n  Pipeline Plan:" -ForegroundColor Cyan
    Write-Host "    Step 1: Extract & Translate dialogues -> translations.json" -ForegroundColor White
    Write-Host "    Step 2: ComfyUI inpaint translated text into comic pages" -ForegroundColor White
    Write-Host "    Folder: $folder" -ForegroundColor DarkGray
    Write-Host "    Workflow: $workflow" -ForegroundColor DarkGray

    if (-not (Prompt-ConfirmExecution "Start Full Pipeline?")) { return }

    # Step 1: Translate
    Write-Host "`n>>> [STEP 1/2] RUNNING TRANSLATION..." -ForegroundColor Magenta
    $transSuccess = Invoke-PipelineScript -ScriptName "llama-translate.py" -Arguments @($folder, "--lang", $lang)
    if (-not $transSuccess) {
        Write-Host "`n  [!] Translation reported errors. Please inspect the output above." -ForegroundColor Yellow
        if (-not (Prompt-ConfirmExecution "Proceed to ComfyUI Inpainting anyway?")) { return }
    }

    # Step 2: Comfy Inpaint
    Write-Host "`n>>> [STEP 2/2] RUNNING COMFYUI INPAINTING..." -ForegroundColor Magenta
    $null = Invoke-PipelineScript -ScriptName "edit_comics_comfy.py" -Arguments @($folder, $workflow, "--lang", $lang)
}

# 5. End-to-End Pipeline: Translate + SD
function Action-PipelineSD {
    Write-Host "`n--- [5] END-TO-END PIPELINE: TRANSLATE + SD SERVER ---" -ForegroundColor Yellow

    $folder = Select-WindowsFolder -Title "Select Comic Folder to Process" -InitialFolder $Global:State.LastComicFolder
    if (-not $folder) { return }

    $lang = Prompt-Language -DefaultLang $Global:State.LastLanguage
    $Global:State.LastLanguage = $lang
    Save-SavedState $Global:State

    Write-Host "`n  Pipeline Plan:" -ForegroundColor Cyan
    Write-Host "    Step 1: Extract & Translate dialogues -> translations.json" -ForegroundColor White
    Write-Host "    Step 2: SD Server inpaint translated text into comic pages" -ForegroundColor White
    Write-Host "    Folder: $folder" -ForegroundColor DarkGray

    if (-not (Prompt-ConfirmExecution "Start Full Pipeline?")) { return }

    # Step 1: Translate
    Write-Host "`n>>> [STEP 1/2] RUNNING TRANSLATION..." -ForegroundColor Magenta
    $transSuccess = Invoke-PipelineScript -ScriptName "llama-translate.py" -Arguments @($folder, "--lang", $lang)
    if (-not $transSuccess) {
        Write-Host "`n  [!] Translation reported errors. Please inspect the output above." -ForegroundColor Yellow
        if (-not (Prompt-ConfirmExecution "Proceed to SD Inpainting anyway?")) { return }
    }

    # Step 2: SD Inpaint
    Write-Host "`n>>> [STEP 2/2] RUNNING SD SERVER INPAINTING..." -ForegroundColor Magenta
    $null = Invoke-PipelineScript -ScriptName "edit_comics.py" -Arguments @($folder, "--lang", $lang)
}

# 6. Export Prompts Only
function Action-ExportPrompts {
    Write-Host "`n--- [6] EXPORT PROMPTS ONLY ---" -ForegroundColor Yellow

    $folder = Select-WindowsFolder -Title "Select Comic Folder with translations.json" -InitialFolder $Global:State.LastComicFolder
    if (-not $folder) { return }

    $transJson = Join-Path $folder "translations.json"
    if (-not (Test-Path $transJson)) {
        Write-Host "  [!] translations.json not found in '$folder'." -ForegroundColor Red
        return
    }

    $lang = Prompt-Language -DefaultLang $Global:State.LastLanguage
    $outFile = Read-Host "  Output filename (Default: prompts.txt inside comic folder)"
    if ([string]::IsNullOrWhiteSpace($outFile)) { $outFile = "prompts.txt" }

    $argsList = @($folder, "--lang", $lang, "--export-only", "--export-prompts", $outFile)
    $null = Invoke-PipelineScript -ScriptName "edit_comics.py" -Arguments $argsList
}

# 7. Test Server Connections
function Action-TestServers {
    Write-Host "`n--- [7] TEST API SERVER CONNECTIONS ---" -ForegroundColor Yellow

    $endpoints = @(
        @{ Name = "llama-server Vision API"; Url = "http://localhost:8080/v1/models" },
        @{ Name = "ComfyUI Server";          Url = "http://localhost:8188/system_stats" },
        @{ Name = "SD Server API";           Url = "http://localhost:8080/v1/models" }
    )

    Write-Host ""
    foreach ($ep in $endpoints) {
        Write-Host ("  Checking {0,-25} ({1})... " -f $ep.Name, $ep.Url) -NoNewline
        try {
            $req = [System.Net.WebRequest]::Create($ep.Url)
            $req.Timeout = 3000
            $req.Method = "GET"
            $resp = $req.GetResponse()
            Write-Host "[ ONLINE ]" -ForegroundColor Green
            $resp.Close()
        } catch [System.Net.WebException] {
            if ($_.Response -ne $null) {
                Write-Host "[ LISTENING ]" -ForegroundColor Green
            } else {
                Write-Host "[ OFFLINE ]" -ForegroundColor Red
            }
        } catch {
            Write-Host "[ OFFLINE ]" -ForegroundColor Red
        }
    }
    Write-Host ""
}

# 8. Open Folder in Explorer
function Action-OpenExplorer {
    Write-Host "`n--- [8] OPEN FOLDER IN FILE EXPLORER ---" -ForegroundColor Yellow
    Write-Host "    [1] Open Comic Folder" -ForegroundColor Cyan
    Write-Host "    [2] Open 'edited\' Subfolder" -ForegroundColor Cyan
    Write-Host "    [3] Open Project Directory ($ScriptDir)" -ForegroundColor Cyan
    Write-Host "    [4] Pick another folder to open" -ForegroundColor Cyan

    $choice = Read-Host "  Select option [1-4] (Default: 1)"
    if ([string]::IsNullOrWhiteSpace($choice)) { $choice = "1" }

    switch ($choice.Trim()) {
        "1" {
            if ($Global:State.LastComicFolder -and (Test-Path $Global:State.LastComicFolder)) {
                Start-Process explorer.exe $Global:State.LastComicFolder
            } else {
                $f = Select-WindowsFolder
                if ($f) { Start-Process explorer.exe $f }
            }
        }
        "2" {
            $folder = $Global:State.LastComicFolder
            if (-not $folder -or -not (Test-Path $folder)) {
                $folder = Select-WindowsFolder
            }
            if ($folder) {
                $editedPath = Join-Path $folder "edited"
                if (Test-Path $editedPath) {
                    Start-Process explorer.exe $editedPath
                } else {
                    Write-Host "  'edited' subfolder does not exist yet. Opening comic folder instead." -ForegroundColor Yellow
                    Start-Process explorer.exe $folder
                }
            }
        }
        "3" {
            Start-Process explorer.exe $ScriptDir
        }
        "4" {
            $f = Select-WindowsFolder
            if ($f) { Start-Process explorer.exe $f }
        }
    }
}

# ==============================================================================
# MAIN MENU LOOP
# ==============================================================================
function Show-MainMenu {
    Clear-Host

    $lastFolderDisplay = if ($Global:State.LastComicFolder) { $Global:State.LastComicFolder } else { "(None selected yet)" }
    $lastWfDisplay     = if ($Global:State.LastWorkflowFile) { Split-Path $Global:State.LastWorkflowFile -Leaf } else { "(None)" }
    $lastLangDisplay   = $Global:State.LastLanguage

    Write-Host "================================================================================" -ForegroundColor Cyan
    Write-Host "              COMIC TRANSLATION & INPAINTING WORKSPACE                          " -ForegroundColor Yellow
    Write-Host "================================================================================" -ForegroundColor Cyan
    Write-Host "  Project Dir : $ScriptDir" -ForegroundColor DarkGray
    Write-Host "  Active Comic: $lastFolderDisplay" -ForegroundColor DarkCyan
    Write-Host "  Workflow    : $lastWfDisplay" -ForegroundColor DarkCyan
    Write-Host "  Language    : $lastLangDisplay" -ForegroundColor DarkCyan
    Write-Host "--------------------------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "  PIPELINE ACTIONS:" -ForegroundColor White
    Write-Host "    [1] 🌐 Translate Comic Pages            (llama-translate.py)" -ForegroundColor Cyan
    Write-Host "    [2] 🧩 Inpaint via ComfyUI API          (edit_comics_comfy.py)" -ForegroundColor Cyan
    Write-Host "    [3] 🎨 Inpaint via SD Server API        (edit_comics.py)" -ForegroundColor Cyan
    Write-Host "    [4] 🚀 Full Pipeline: Translate + ComfyUI" -ForegroundColor Green
    Write-Host "    [5] ⚡ Full Pipeline: Translate + SD Server" -ForegroundColor Green
    Write-Host "    [6] 📝 Export Prompts Only              (--export-only)" -ForegroundColor Cyan
    Write-Host "--------------------------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "  UTILITIES:" -ForegroundColor White
    Write-Host "    [7] 🔌 Test Server Connections (Llama / ComfyUI / SD)" -ForegroundColor White
    Write-Host "    [8] 📂 Open Comic / Edited Output in Windows Explorer" -ForegroundColor White
    Write-Host "    [R] 🔄 Reset / Change Active Comic Folder" -ForegroundColor DarkYellow
    Write-Host "    [Q] 🚪 Exit" -ForegroundColor Red
    Write-Host "================================================================================" -ForegroundColor Cyan
}

# --- Loop ---
while ($true) {
    Show-MainMenu
    $choice = Read-Host "`n  Select an option"
    if ([string]::IsNullOrWhiteSpace($choice)) { continue }

    switch ($choice.Trim().ToUpper()) {
        "1" { Action-Translate; Pause-Menu }
        "2" { Action-InpaintComfy; Pause-Menu }
        "3" { Action-InpaintSD; Pause-Menu }
        "4" { Action-PipelineComfy; Pause-Menu }
        "5" { Action-PipelineSD; Pause-Menu }
        "6" { Action-ExportPrompts; Pause-Menu }
        "7" { Action-TestServers; Pause-Menu }
        "8" { Action-OpenExplorer; Pause-Menu }
        "R" {
            $f = Select-WindowsFolder -Title "Set Active Comic Folder" -InitialFolder $Global:State.LastComicFolder
            if ($f) {
                $Global:State.LastComicFolder = $f
                Save-SavedState $Global:State
            }
        }
        "Q" {
            Write-Host "`n  Goodbye!`n" -ForegroundColor Cyan
            exit 0
        }
        default {
            Write-Host "  Invalid choice. Please enter a valid number or Q." -ForegroundColor Red
            Start-Sleep -Seconds 1
        }
    }
}
