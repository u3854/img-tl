<#
.SYNOPSIS
    Interactive Launcher & Workflow Menu for Comic Translation & Inpainting Pipeline.
.DESCRIPTION
    Runs from anywhere on the system. Uses native Windows GUI pickers for file/folder selection.
    Dynamically reads default settings from common.py and orchestrates pipeline scripts.
#>

# Enable UTF-8 console output for beautiful unicode characters
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

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
        if ($Global:State) {
            $Global:State.LastComicFolder = $selected
            Save-SavedState $Global:State
        }
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
# RESOLVE PACKAGE DIRECTORY (DYNAMICALLY DETECTED - NO HARDCODED PATHS)
# ==============================================================================
$UserConfigFile = Join-Path $HOME ".img_tl_config.json"

function Resolve-PackageDir {
    # 1. Standard repo root detection (script inside repo)
    $candidate = if ($PSScriptRoot) {
        $PSScriptRoot
    } elseif ($MyInvocation.MyCommand.Path) {
        Split-Path -Parent $MyInvocation.MyCommand.Path
    } else {
        $PWD.Path
    }

    if ($candidate -and (Test-Path (Join-Path $candidate "common.py"))) {
        return (Resolve-Path $candidate).Path
    }

    # 2. Check environment variable
    if ($env:IMG_TL_DIR -and (Test-Path (Join-Path $env:IMG_TL_DIR "common.py"))) {
        return (Resolve-Path $env:IMG_TL_DIR).Path
    }

    # 3. Check persistent user config file (~/.img_tl_config.json)
    if (Test-Path $UserConfigFile) {
        try {
            $cfg = Get-Content $UserConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($cfg.PackageDir -and (Test-Path (Join-Path $cfg.PackageDir "common.py"))) {
                return (Resolve-Path $cfg.PackageDir).Path
            }
        } catch {}
    }

    # 4. If script was copied outside repo (e.g. C:\Shortcuts), prompt user with Windows Folder Picker once!
    Write-Host "`n  [i] 'common.py' not found in '$candidate'." -ForegroundColor Yellow
    Write-Host "  Please select your 'img-tl' repository folder using the Windows folder picker..." -ForegroundColor Cyan

    $picked = Select-WindowsFolder -Title "Select 'img-tl' Repository Folder"
    if ($picked -and (Test-Path (Join-Path $picked "common.py"))) {
        $cleanPath = (Resolve-Path $picked).Path
        try {
            [PSCustomObject]@{ PackageDir = $cleanPath } | ConvertTo-Json | Set-Content -Path $UserConfigFile -Encoding UTF8
            Write-Host "  [✓] Repository location saved to $UserConfigFile" -ForegroundColor Green
        } catch {}
        return $cleanPath
    }

    Write-Host "`n  [ERROR] Could not locate the 'img-tl' repository." -ForegroundColor Red
    return $null
}

$ScriptDir = Resolve-PackageDir
if (-not $ScriptDir) {
    Write-Host "  Exiting.`n" -ForegroundColor Red
    exit 1
}

# ==============================================================================
# DYNAMIC DEFAULTS EXTRACTOR (FROM common.py)
# ==============================================================================
function Get-PipelineDefaults {
    param([string]$RepoDir)

    $commonPy = Join-Path $RepoDir "common.py"
    if (-not (Test-Path $commonPy)) {
        return [PSCustomObject]@{
            DEFAULT_LANGUAGE        = "chinese"
            DEFAULT_DELAY           = 120.0
            DEFAULT_COMFY_DELAY     = 120.0
            DEFAULT_THINKING_EFFORT = "low"
            DEFAULT_THINKING_BUDGET = 1024
            DEFAULT_STEPS           = 4
            DEFAULT_CFG             = 1.0
            DEFAULT_MAX_SIDE        = 1280
            DEFAULT_SAMPLER         = "euler"
            DEFAULT_SCHEDULER       = "simple"
            DEFAULT_HOST            = "http://localhost"
            DEFAULT_LLAMA_PORT      = 8080
            DEFAULT_SD_PORT         = 8080
            DEFAULT_COMFY_PORT      = 8188
            DEFAULT_LLAMA_API_URL   = "http://localhost:8080/v1/chat/completions"
            DEFAULT_SD_API_URL      = "http://localhost:8080/v1/images/edits"
            DEFAULT_COMFY_API_URL   = "http://localhost:8188"
            DEFAULT_OUTPUT_FILENAME = "translations.json"
            DEFAULT_OUTPUT_DIR      = "edited"
            DEFAULT_EXPORT_FILENAME = "prompts.txt"
            DEFAULT_TIMEOUT         = 600
            DEFAULT_RETRIES         = 2
        }
    }

    # 1. Fast Python introspection via .venv or uv
    $venvPy = Join-Path $RepoDir ".venv\Scripts\python.exe"
    $uvExe = Get-Command "uv" -ErrorAction SilentlyContinue
    $pyCode = "import common, json; d = {k: getattr(common, k) for k in dir(common) if k.startswith(`"DEFAULT_`")}; print(json.dumps(d))"

    $jsonRaw = $null
    if (Test-Path $venvPy) {
        try {
            $jsonRaw = & "$venvPy" -c $pyCode 2>$null
        } catch {}
    } elseif ($uvExe) {
        try {
            $jsonRaw = & $uvExe.Source run --project "$RepoDir" python -c $pyCode 2>$null
        } catch {}
    }

    if ($jsonRaw) {
        try {
            $obj = $jsonRaw | ConvertFrom-Json
            if ($obj.DEFAULT_LANGUAGE) { return $obj }
        } catch {}
    }

    # 2. Fast regex fallback directly from common.py
    $map = @{
        DEFAULT_LANGUAGE        = "chinese"
        DEFAULT_DELAY           = 60.0
        DEFAULT_COMFY_DELAY     = 120.0
        DEFAULT_THINKING_EFFORT = "low"
        DEFAULT_THINKING_BUDGET = 1024
        DEFAULT_STEPS           = 4
        DEFAULT_CFG             = 1.0
        DEFAULT_MAX_SIDE        = 1280
        DEFAULT_SAMPLER         = "euler"
        DEFAULT_SCHEDULER       = "simple"
        DEFAULT_HOST            = "http://localhost"
        DEFAULT_LLAMA_PORT      = 8080
        DEFAULT_SD_PORT         = 8080
        DEFAULT_COMFY_PORT      = 8188
        DEFAULT_OUTPUT_FILENAME = "translations.json"
        DEFAULT_OUTPUT_DIR      = "edited"
        DEFAULT_EXPORT_FILENAME = "prompts.txt"
        DEFAULT_TIMEOUT         = 600
        DEFAULT_RETRIES         = 2
    }

    Get-Content $commonPy -Encoding UTF8 | ForEach-Object {
        if ($_ -match "^\s*(DEFAULT_[A-Z0-9_]+)\s*=\s*(.*)$") {
            $k = $matches[1]
            $v = $matches[2].Trim()
            if ($v -match "^(.*?)\s*#.*$") { $v = $matches[1].Trim() }
            if ($v -match "^[\x22\x27](.*)[\x22\x27]$") {
                $map[$k] = $matches[1]
            } elseif ($v -match "^-?\d+$") {
                $map[$k] = [int]$v
            } elseif ($v -match "^-?\d+\.\d+$") {
                $map[$k] = [double]$v
            }
        }
    }

    $h = $map["DEFAULT_HOST"]
    $map["DEFAULT_LLAMA_API_URL"] = "$h`:$($map.DEFAULT_LLAMA_PORT)/v1/chat/completions"
    $map["DEFAULT_SD_API_URL"]    = "$h`:$($map.DEFAULT_SD_PORT)/v1/images/edits"
    $map["DEFAULT_COMFY_API_URL"] = "$h`:$($map.DEFAULT_COMFY_PORT)"

    return [PSCustomObject]$map
}

# Dynamically load defaults from common.py
$Global:Defaults = Get-PipelineDefaults -RepoDir $ScriptDir

$StateFile = Join-Path $ScriptDir ".menu_state.json"

# ==============================================================================
# STATE MANAGEMENT
# ==============================================================================
function Get-SavedState {
    if (Test-Path $StateFile) {
        try {
            $json = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($json) {
                # Ensure defaults fill in any missing fields
                if (-not $json.LastLanguage) { $json | Add-Member -NotePropertyName "LastLanguage" -NotePropertyValue $Global:Defaults.DEFAULT_LANGUAGE -Force }
                if (-not $json.LastThinkingEffort) { $json | Add-Member -NotePropertyName "LastThinkingEffort" -NotePropertyValue $Global:Defaults.DEFAULT_THINKING_EFFORT -Force }
                if (-not $json.LastDelay) { $json | Add-Member -NotePropertyName "LastDelay" -NotePropertyValue $Global:Defaults.DEFAULT_DELAY -Force }
                return $json
            }
        } catch {}
    }
    return [PSCustomObject]@{
        LastComicFolder    = ""
        LastWorkflowFile   = (Join-Path $ScriptDir "qwen21-gguf.json")
        LastLanguage       = $Global:Defaults.DEFAULT_LANGUAGE
        LastThinkingEffort = $Global:Defaults.DEFAULT_THINKING_EFFORT
        LastDelay          = $Global:Defaults.DEFAULT_DELAY
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
    param([string]$DefaultLang = "")

    if (-not $DefaultLang) {
        $DefaultLang = $Global:Defaults.DEFAULT_LANGUAGE
    }

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

    $activeLang = if ($Global:State.LastLanguage) { $Global:State.LastLanguage } else { $Global:Defaults.DEFAULT_LANGUAGE }
    $lang = Prompt-Language -DefaultLang $activeLang
    $Global:State.LastLanguage = $lang
    Save-SavedState $Global:State

    $defEffort = $Global:Defaults.DEFAULT_THINKING_EFFORT
    $defDelay  = $Global:Defaults.DEFAULT_DELAY

    Write-Host "`n  Configuration Mode:" -ForegroundColor White
    Write-Host "    [1] Quick Run (Recommended: $defEffort thinking effort, ${defDelay}s delay)" -ForegroundColor Green
    Write-Host "    [2] Customize settings (Thinking effort, delay, skip errors, etc.)" -ForegroundColor Cyan
    $mode = Read-Host "  Choose mode [1/2] (Default: 1)"
    if ([string]::IsNullOrWhiteSpace($mode)) { $mode = "1" }

    $argsList = @($folder, "--lang", $lang)

    if ($mode.Trim() -eq "2") {
        Write-Host "`n  Thinking Effort Level:" -ForegroundColor White
        Write-Host "    [1] low  [2] medium  [3] high  [4] none" -ForegroundColor Cyan
        $effPrompt = Read-Host "  Select effort [1-4] (Default: $defEffort)"
        $effortMap = @{ "1"="low"; "2"="medium"; "3"="high"; "4"="none" }
        $chosenEffort = if ($effortMap.ContainsKey($effPrompt)) { $effortMap[$effPrompt] } else { $defEffort }
        $argsList += @("--thinking-effort", $chosenEffort)

        $delayInput = Read-Host "  Delay between API calls in seconds (Default: $defDelay)"
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

    $activeLang = if ($Global:State.LastLanguage) { $Global:State.LastLanguage } else { $Global:Defaults.DEFAULT_LANGUAGE }
    $lang = Prompt-Language -DefaultLang $activeLang
    $Global:State.LastLanguage = $lang
    Save-SavedState $Global:State

    $defComfyDelay = $Global:Defaults.DEFAULT_COMFY_DELAY
    Write-Host "`n  Options:" -ForegroundColor White
    $overwrite = Read-Host "  Overwrite existing images in 'edited\' subfolder? [y/N]"
    $delayInput = Read-Host "  Delay between ComfyUI calls in seconds (Default: $defComfyDelay)"

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

    $activeLang = if ($Global:State.LastLanguage) { $Global:State.LastLanguage } else { $Global:Defaults.DEFAULT_LANGUAGE }
    $lang = Prompt-Language -DefaultLang $activeLang
    $Global:State.LastLanguage = $lang
    Save-SavedState $Global:State

    $defSteps   = $Global:Defaults.DEFAULT_STEPS
    $defCfg     = $Global:Defaults.DEFAULT_CFG
    $defMaxSide = $Global:Defaults.DEFAULT_MAX_SIDE
    $defDelay   = $Global:Defaults.DEFAULT_DELAY

    Write-Host "`n  Configuration Mode:" -ForegroundColor White
    Write-Host "    [1] Quick Run (Steps: $defSteps, CFG: $defCfg, Max Side: ${defMaxSide}px, Delay: ${defDelay}s)" -ForegroundColor Green
    Write-Host "    [2] Customize parameters" -ForegroundColor Cyan
    $mode = Read-Host "  Choose mode [1/2] (Default: 1)"
    if ([string]::IsNullOrWhiteSpace($mode)) { $mode = "1" }

    $argsList = @($targetInput, "--lang", $lang)

    if ($mode.Trim() -eq "2") {
        $steps = Read-Host "  Sampling steps (Default: $defSteps for Turbo)"
        if ($steps -and [int]::TryParse($steps, [ref]$null)) { $argsList += @("--steps", $steps.Trim()) }

        $cfg = Read-Host "  CFG scale (Default: $defCfg for Turbo)"
        if ($cfg -and [double]::TryParse($cfg, [ref]$null)) { $argsList += @("--cfg", $cfg.Trim()) }

        $maxSide = Read-Host "  Max image side in pixels (Default: $defMaxSide)"
        if ($maxSide -and [int]::TryParse($maxSide, [ref]$null)) { $argsList += @("--max-side", $maxSide.Trim()) }

        $delay = Read-Host "  Delay between edits in seconds (Default: $defDelay)"
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

    $activeLang = if ($Global:State.LastLanguage) { $Global:State.LastLanguage } else { $Global:Defaults.DEFAULT_LANGUAGE }
    $lang = Prompt-Language -DefaultLang $activeLang
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

    $activeLang = if ($Global:State.LastLanguage) { $Global:State.LastLanguage } else { $Global:Defaults.DEFAULT_LANGUAGE }
    $lang = Prompt-Language -DefaultLang $activeLang
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

    $activeLang = if ($Global:State.LastLanguage) { $Global:State.LastLanguage } else { $Global:Defaults.DEFAULT_LANGUAGE }
    $lang = Prompt-Language -DefaultLang $activeLang

    $defExport = $Global:Defaults.DEFAULT_EXPORT_FILENAME
    $outFile = Read-Host "  Output filename (Default: $defExport inside comic folder)"
    if ([string]::IsNullOrWhiteSpace($outFile)) { $outFile = $defExport }

    $argsList = @($folder, "--lang", $lang, "--export-only", "--export-prompts", $outFile)
    $null = Invoke-PipelineScript -ScriptName "edit_comics.py" -Arguments $argsList
}

# 7. Test Server Connections
function Action-TestServers {
    Write-Host "`n--- [7] TEST API SERVER CONNECTIONS ---" -ForegroundColor Yellow

    $h = $Global:Defaults.DEFAULT_HOST
    $llamaTestUrl = "$h`:$($Global:Defaults.DEFAULT_LLAMA_PORT)/v1/models"
    $comfyTestUrl = "$($Global:Defaults.DEFAULT_COMFY_API_URL)/system_stats"
    $sdTestUrl    = "$h`:$($Global:Defaults.DEFAULT_SD_PORT)/v1/models"

    $endpoints = @(
        @{ Name = "llama-server Vision API"; Url = $llamaTestUrl },
        @{ Name = "ComfyUI Server";          Url = $comfyTestUrl },
        @{ Name = "SD Server API";           Url = $sdTestUrl }
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
                # Got HTTP response (even 404/405 means port is listening)
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

# 9. View Dynamic Defaults
function Action-ViewDefaults {
    Write-Host "`n--- CURRENT DEFAULTS (Loaded dynamically from common.py) ---" -ForegroundColor Yellow
    Write-Host ""
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_LANGUAGE", $Global:Defaults.DEFAULT_LANGUAGE) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}s" -f "DEFAULT_DELAY", $Global:Defaults.DEFAULT_DELAY) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}s" -f "DEFAULT_COMFY_DELAY", $Global:Defaults.DEFAULT_COMFY_DELAY) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_THINKING_EFFORT", $Global:Defaults.DEFAULT_THINKING_EFFORT) -ForegroundColor White
    Write-Host ("  {0,-26} : {1} tokens" -f "DEFAULT_THINKING_BUDGET", $Global:Defaults.DEFAULT_THINKING_BUDGET) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_STEPS", $Global:Defaults.DEFAULT_STEPS) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_CFG", $Global:Defaults.DEFAULT_CFG) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}px" -f "DEFAULT_MAX_SIDE", $Global:Defaults.DEFAULT_MAX_SIDE) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_SAMPLER", $Global:Defaults.DEFAULT_SAMPLER) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_SCHEDULER", $Global:Defaults.DEFAULT_SCHEDULER) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_HOST", $Global:Defaults.DEFAULT_HOST) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_LLAMA_PORT", $Global:Defaults.DEFAULT_LLAMA_PORT) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_SD_PORT", $Global:Defaults.DEFAULT_SD_PORT) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_COMFY_PORT", $Global:Defaults.DEFAULT_COMFY_PORT) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_TIMEOUT", $Global:Defaults.DEFAULT_TIMEOUT) -ForegroundColor White
    Write-Host ("  {0,-26} : {1}" -f "DEFAULT_RETRIES", $Global:Defaults.DEFAULT_RETRIES) -ForegroundColor White
    Write-Host ""
    Write-Host "  [i] Edit common.py anytime to change any of these defaults." -ForegroundColor DarkGray
}

# ==============================================================================
# MAIN MENU LOOP
# ==============================================================================
function Show-MainMenu {
    Clear-Host

    $lastFolderDisplay = if ($Global:State.LastComicFolder) { $Global:State.LastComicFolder } else { "(None selected yet)" }
    $lastWfDisplay     = if ($Global:State.LastWorkflowFile) { Split-Path $Global:State.LastWorkflowFile -Leaf } else { "(None)" }
    $lastLangDisplay   = if ($Global:State.LastLanguage) { $Global:State.LastLanguage } else { $Global:Defaults.DEFAULT_LANGUAGE }

    Write-Host "================================================================================" -ForegroundColor Cyan
    Write-Host "              COMIC TRANSLATION & INPAINTING WORKSPACE                          " -ForegroundColor Yellow
    Write-Host "================================================================================" -ForegroundColor Cyan
    Write-Host "  Project Dir : $ScriptDir" -ForegroundColor DarkGray
    Write-Host "  Active Comic: $lastFolderDisplay" -ForegroundColor DarkCyan
    Write-Host "  Workflow    : $lastWfDisplay" -ForegroundColor DarkCyan
    Write-Host "  Language    : $lastLangDisplay" -ForegroundColor DarkCyan
    Write-Host "  Delays      : API=$($Global:Defaults.DEFAULT_DELAY)s | Comfy=$($Global:Defaults.DEFAULT_COMFY_DELAY)s" -ForegroundColor DarkGray
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
    Write-Host "    [S] ⚙️  View Current Defaults (from common.py)" -ForegroundColor White
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
        "S" { Action-ViewDefaults; Pause-Menu }
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
