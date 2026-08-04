param(
    [Parameter(Mandatory = $true)][string]$FrozenManifestPath,
    [Parameter(Mandatory = $true)][string]$FrozenBarsPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$replay = Join-Path $here 'replay.ps1'
$scratch = Join-Path $here 'test-scratch'

function Assert($Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

function Write-Utf8([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function New-Manifest([int]$Maximum = 25, [int]$MinimumIndex = 0) {
    return [ordered]@{
        manifest_version = 1; symbol = 'TEST'; direction = 'long'; input_file = 'bars.csv'
        input_columns = @('index', 'timestamp_utc', 'close')
        fast_window = 2; slow_window = 3; mean_round_decimal_places = 9
        signal_rule = 'sma_fast > sma_slow AND close < sma_fast'
        edge_rule = 'emit only when signal_rule is true on this bar and false on the immediately previous bar'
        minimum_signal_index = $MinimumIndex; decision_offset_seconds = 1
        execution_rule = 'the next input bar timestamp; if absent, decision timestamp plus 60 seconds'
        maximum_outputs = $Maximum; output_file = 'oracle-output.json'; output_schema = 'output-schema.json'
    }
}

function Write-Fixture([string]$Name, $Manifest, [string[]]$Rows) {
    $dir = Join-Path $scratch $Name
    [IO.Directory]::CreateDirectory($dir) | Out-Null
    $manifestPath = Join-Path $dir 'manifest.json'
    $barsPath = Join-Path $dir 'bars.csv'
    $outputPath = Join-Path $dir 'out.json'
    Write-Utf8 $manifestPath (($Manifest | ConvertTo-Json -Depth 5) + "`n")
    Write-Utf8 $barsPath ((@('index,timestamp_utc,close') + $Rows) -join "`n")
    return @($manifestPath, $barsPath, $outputPath)
}

function Invoke-Replay([string[]]$Fixture) {
    & $replay -ManifestPath $Fixture[0] -BarsPath $Fixture[1] -OutputPath $Fixture[2]
    return Get-Content -Raw -LiteralPath $Fixture[2] | ConvertFrom-Json
}

function Assert-OutputShape($Output) {
    $top = @($Output.psobject.Properties.Name | Sort-Object)
    Assert (($top -join ',') -ceq 'bars_sha256,manifest_sha256,signals') 'top-level schema properties'
    Assert ($Output.manifest_sha256 -cmatch '^sha256:[0-9a-f]{64}$') 'manifest digest format'
    Assert ($Output.bars_sha256 -cmatch '^sha256:[0-9a-f]{64}$') 'bars digest format'
    Assert (@($Output.signals).Count -le 25) 'schema maximum signal count'
    foreach ($signal in @($Output.signals)) {
        $keys = @($signal.psobject.Properties.Name | Sort-Object)
        Assert (($keys -join ',') -ceq 'close,decision_at_utc,earliest_execution_at_utc,index,pullback,sma_fast,sma_slow,uptrend') 'signal schema properties'
        Assert ($signal.index -is [int] -or $signal.index -is [long]) 'index is integer'
        Assert ($signal.uptrend -is [bool] -and $signal.pullback -is [bool]) 'flags are boolean'
    }
}

if ([IO.Directory]::Exists($scratch)) { [IO.Directory]::Delete($scratch, $true) }
[IO.Directory]::CreateDirectory($scratch) | Out-Null

try {
    # Cold start: a corpus shorter than the slow window cannot produce a signal.
    $cold = Write-Fixture 'cold' (New-Manifest) @(
        '0,2026-01-01T00:00:00+00:00,1',
        '1,2026-01-01T00:01:00+00:00,3'
    )
    $coldOut = Invoke-Replay $cold
    Assert (@($coldOut.signals).Count -eq 0) 'cold start emits no signal'

    # Rising edges: [1,3,2] creates a signal; a repeated block must create one new edge.
    $edge = Write-Fixture 'edges' (New-Manifest) @(
        '0,2026-01-01T00:00:00+00:00,1', '1,2026-01-01T00:01:00+00:00,3',
        '2,2026-01-01T00:02:00+00:00,2', '3,2026-01-01T00:03:00+00:00,1',
        '4,2026-01-01T00:04:00+00:00,3', '5,2026-01-01T00:05:00+00:00,2'
    )
    $edgeOut = Invoke-Replay $edge
    Assert ((@($edgeOut.signals.index) -join ',') -ceq '2,5') 'only rising edges are emitted'
    Assert ($edgeOut.signals[0].decision_at_utc -ceq '2026-01-01T00:02:01+00:00') 'decision offset is applied'
    Assert ($edgeOut.signals[1].earliest_execution_at_utc -ceq '2026-01-01T00:06:01+00:00') 'last-row execution fallback uses decision plus 60 seconds'

    # Bounds under sustained input: 30 candidate edges are capped at 25.
    $manyRows = [Collections.Generic.List[string]]::new()
    for ($i = 0; $i -lt 90; $i++) {
        $value = @(1, 3, 2)[$i % 3]
        $stamp = ([DateTimeOffset]'2026-01-01T00:00:00+00:00').AddMinutes($i).ToString("yyyy-MM-dd'T'HH:mm:sszzz")
        $manyRows.Add("$i,$stamp,$value")
    }
    $bounded = Write-Fixture 'bounded' (New-Manifest 25) $manyRows.ToArray()
    $boundedOut = Invoke-Replay $bounded
    Assert (@($boundedOut.signals).Count -eq 25) 'maximum_outputs is enforced under sustained candidates'
    Assert-OutputShape $boundedOut

    # Malformed input cases fail closed instead of silently changing the corpus.
    $badCases = @(
        @('bad-index', @('x,2026-01-01T00:00:00+00:00,1')),
        @('bad-columns', @('0,2026-01-01T00:00:00+00:00')),
        @('bad-close', @('0,2026-01-01T00:00:00+00:00,NaN')),
        @('bad-timezone', @('0,2026-01-01T00:00:00-08:00,1')),
        @('unordered', @('1,2026-01-01T00:00:00+00:00,1', '0,2026-01-01T00:01:00+00:00,2'))
    )
    foreach ($case in $badCases) {
        $fixture = Write-Fixture $case[0] (New-Manifest) $case[1]
        $failed = $false
        try { Invoke-Replay $fixture | Out-Null } catch { $failed = $true }
        Assert $failed "malformed case $($case[0]) is rejected"
    }

    # Full frozen corpus: repeat from cold process state and require byte-identical artifacts.
    $frozenOne = Join-Path $scratch 'frozen-one.json'
    $frozenTwo = Join-Path $scratch 'frozen-two.json'
    & $replay -ManifestPath $FrozenManifestPath -BarsPath $FrozenBarsPath -OutputPath $frozenOne
    & $replay -ManifestPath $FrozenManifestPath -BarsPath $FrozenBarsPath -OutputPath $frozenTwo
    $hashOne = (Get-FileHash -Algorithm SHA256 -LiteralPath $frozenOne).Hash
    $hashTwo = (Get-FileHash -Algorithm SHA256 -LiteralPath $frozenTwo).Hash
    Assert ($hashOne -ceq $hashTwo) 'full-corpus output is byte deterministic'
    $fullOut = Get-Content -Raw -LiteralPath $frozenOne | ConvertFrom-Json
    Assert-OutputShape $fullOut
    Assert (@($fullOut.signals).Count -le 25) 'full corpus respects manifest bound'

    Write-Output 'PASS: 5 scenario groups (cold start, rising edges/execution fallback, sustained bounds, malformed rows, full-corpus determinism/schema)'
} finally {
    if ([IO.Directory]::Exists($scratch)) { [IO.Directory]::Delete($scratch, $true) }
}
