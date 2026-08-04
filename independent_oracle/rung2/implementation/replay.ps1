param(
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$BarsPath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-JsonObject([string]$Path) {
    $value = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    if ($null -eq $value) { throw "Manifest is empty: $Path" }
    return $value
}

function Require-Integer($Value, [string]$Name, [int]$Minimum) {
    $parsed = 0
    if (-not [int]::TryParse([string]$Value, [Globalization.NumberStyles]::Integer,
            [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed) -or $parsed -lt $Minimum) {
        throw "$Name must be an integer >= $Minimum"
    }
    return $parsed
}

function Read-Bars([string]$Path, [string[]]$ExpectedColumns) {
    $lines = @(Get-Content -LiteralPath $Path)
    if ($lines.Count -lt 1) { throw 'Bars CSV is empty' }
    $header = $lines[0].TrimStart([char]0xFEFF)
    if ($header -cne ($ExpectedColumns -join ',')) {
        throw "Bars CSV header must be exactly: $($ExpectedColumns -join ',')"
    }

    $rows = [Collections.Generic.List[object]]::new()
    $previousIndex = -1
    for ($lineNumber = 2; $lineNumber -le $lines.Count; $lineNumber++) {
        $line = $lines[$lineNumber - 1]
        if ([string]::IsNullOrWhiteSpace($line)) { throw "Blank CSV row at line $lineNumber" }
        $fields = $line.Split(',')
        if ($fields.Count -ne 3) { throw "Malformed CSV row at line $lineNumber" }

        $index = 0
        if (-not [int]::TryParse($fields[0], [Globalization.NumberStyles]::Integer,
                [Globalization.CultureInfo]::InvariantCulture, [ref]$index) -or $index -lt 0) {
            throw "Invalid index at line $lineNumber"
        }
        if ($index -le $previousIndex) { throw "Indices must be strictly increasing at line $lineNumber" }
        $previousIndex = $index

        $timestamp = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParseExact(
                $fields[1], 'yyyy-MM-ddTHH:mm:sszzz',
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::None, [ref]$timestamp) -or
                $timestamp.Offset -ne [TimeSpan]::Zero) {
            throw "Invalid UTC timestamp at line $lineNumber"
        }

        $close = [decimal]::Zero
        if (-not [decimal]::TryParse($fields[2], [Globalization.NumberStyles]::AllowDecimalPoint,
                [Globalization.CultureInfo]::InvariantCulture, [ref]$close)) {
            throw "Invalid finite close at line $lineNumber"
        }
        $rows.Add([pscustomobject]@{ Index = $index; Timestamp = $timestamp; Close = $close })
    }
    return $rows
}

function Get-FileDigest([string]$Path) {
    return 'sha256:' + (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Format-Utc([DateTimeOffset]$Value) {
    return $Value.ToUniversalTime().ToString(
        "yyyy-MM-dd'T'HH:mm:sszzz", [Globalization.CultureInfo]::InvariantCulture)
}

$manifest = Read-JsonObject $ManifestPath
if ([int]$manifest.manifest_version -ne 1) { throw 'Unsupported manifest_version' }
if ([string]$manifest.direction -cne 'long') { throw 'Only long direction is supported by this frozen replay' }
if ([string]$manifest.signal_rule -cne 'sma_fast > sma_slow AND close < sma_fast') { throw 'Unsupported signal_rule' }
if ([string]$manifest.edge_rule -cne 'emit only when signal_rule is true on this bar and false on the immediately previous bar') { throw 'Unsupported edge_rule' }
if ([string]$manifest.execution_rule -cne 'the next input bar timestamp; if absent, decision timestamp plus 60 seconds') { throw 'Unsupported execution_rule' }

$fastWindow = Require-Integer $manifest.fast_window 'fast_window' 1
$slowWindow = Require-Integer $manifest.slow_window 'slow_window' 1
$places = Require-Integer $manifest.mean_round_decimal_places 'mean_round_decimal_places' 0
$minimumIndex = Require-Integer $manifest.minimum_signal_index 'minimum_signal_index' 0
$decisionOffset = Require-Integer $manifest.decision_offset_seconds 'decision_offset_seconds' 0
$maximumOutputs = Require-Integer $manifest.maximum_outputs 'maximum_outputs' 0
if ($places -gt 28) { throw 'mean_round_decimal_places must be <= 28 for decimal arithmetic' }
if ($fastWindow -ge $slowWindow) { throw 'fast_window must be smaller than slow_window' }
if ($maximumOutputs -gt 25) { throw 'maximum_outputs exceeds the output schema bound of 25' }

$expectedColumns = @($manifest.input_columns | ForEach-Object { [string]$_ })
if ($expectedColumns.Count -ne 3 -or ($expectedColumns -join ',') -cne 'index,timestamp_utc,close') {
    throw 'Unsupported input_columns'
}
$bars = @(Read-Bars $BarsPath $expectedColumns)
$signals = [Collections.Generic.List[object]]::new()
$previousSignal = $false

for ($position = 0; $position -lt $bars.Count; $position++) {
    $hasMeans = $position -ge ($slowWindow - 1)
    $isSignal = $false
    if ($hasMeans) {
        $fastSum = [decimal]::Zero
        for ($j = $position - $fastWindow + 1; $j -le $position; $j++) { $fastSum += $bars[$j].Close }
        $slowSum = [decimal]::Zero
        for ($j = $position - $slowWindow + 1; $j -le $position; $j++) { $slowSum += $bars[$j].Close }
        $smaFast = [Math]::Round($fastSum / $fastWindow, $places, [MidpointRounding]::ToEven)
        $smaSlow = [Math]::Round($slowSum / $slowWindow, $places, [MidpointRounding]::ToEven)
        $uptrend = $smaFast -gt $smaSlow
        $pullback = $bars[$position].Close -lt $smaFast
        $isSignal = $uptrend -and $pullback

        if ($isSignal -and -not $previousSignal -and $bars[$position].Index -ge $minimumIndex -and
                $signals.Count -lt $maximumOutputs) {
            $decisionAt = $bars[$position].Timestamp.AddSeconds($decisionOffset)
            if ($position + 1 -lt $bars.Count) {
                $executionAt = $bars[$position + 1].Timestamp
            } else {
                $executionAt = $decisionAt.AddSeconds(60)
            }
            $signals.Add([ordered]@{
                index = $bars[$position].Index
                decision_at_utc = Format-Utc $decisionAt
                earliest_execution_at_utc = Format-Utc $executionAt
                close = $bars[$position].Close
                sma_fast = $smaFast
                sma_slow = $smaSlow
                uptrend = $uptrend
                pullback = $pullback
            })
        }
    }
    $previousSignal = $isSignal
}

$result = [ordered]@{
    manifest_sha256 = Get-FileDigest $ManifestPath
    bars_sha256 = Get-FileDigest $BarsPath
    signals = $signals
}
$json = $result | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($OutputPath, $json + "`n", [Text.UTF8Encoding]::new($false))
