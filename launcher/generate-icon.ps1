<#
.SYNOPSIS
    Genera l'icona multi-risoluzione `launcher\local-llm.ico` (256, 48, 32, 16 px)
    usando esclusivamente .NET System.Drawing (zero dipendenze esterne, 100% offline).
#>

[CmdletBinding()]
param(
    [string]$OutPath = (Join-Path $PSScriptRoot "local-llm.ico")
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

function New-IconPngBytes([int]$Size) {
    $bmp = New-Object System.Drawing.Bitmap($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    try {
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $g.Clear([System.Drawing.Color]::Transparent)

        $pad = [float]($Size * 0.05)
        $w = [float]($Size - 2 * $pad)
        $r = [float]($w * 0.24)

        # Rounded squircle path
        $path = New-Object System.Drawing.Drawing2D.GraphicsPath
        $d = $r * 2
        $path.AddArc($pad, $pad, $d, $d, 180, 90)
        $path.AddArc($pad + $w - $d, $pad, $d, $d, 270, 90)
        $path.AddArc($pad + $w - $d, $pad + $w - $d, $d, $d, 0, 90)
        $path.AddArc($pad, $pad + $w - $d, $d, $d, 90, 90)
        $path.CloseFigure()

        # Deep obsidian gradient background
        $rect = New-Object System.Drawing.RectangleF($pad, $pad, $w, $w)
        $bgBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
            $rect,
            [System.Drawing.Color]::FromArgb(255, 13, 18, 28),
            [System.Drawing.Color]::FromArgb(255, 22, 32, 48),
            45.0
        )
        $g.FillPath($bgBrush, $path)

        # Subtle glowing border
        $borderWidth = [math]::Max(1.0, $Size * 0.03)
        $borderPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(220, 63, 185, 80), $borderWidth)
        $g.DrawPath($borderPen, $path)

        # Outer neural hexagon
        $cx = [float]($Size / 2.0)
        $cy = [float]($Size / 2.0)
        $hexR = [float]($Size * 0.30)
        $pts = New-Object System.Drawing.PointF[] 6
        for ($i = 0; $i -lt 6; $i++) {
            $angle = ([math]::PI / 180.0) * (60 * $i - 90)
            $pts[$i] = New-Object System.Drawing.PointF(
                [float]($cx + $hexR * [math]::Cos($angle)),
                [float]($cy + $hexR * [math]::Sin($angle))
            )
        }

        # Connection spokes from center to vertices
        $spokePen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(140, 88, 166, 255), [math]::Max(1.0, $Size * 0.028))
        foreach ($pt in $pts) {
            $g.DrawLine($spokePen, $cx, $cy, $pt.X, $pt.Y)
        }

        # Hexagon ring
        $hexPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(235, 88, 166, 255), [math]::Max(1.2, $Size * 0.042))
        $hexPen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $g.DrawPolygon($hexPen, $pts)

        # Vertex nodes
        $nodeR = [float]([math]::Max(1.4, $Size * 0.055))
        $nodeBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 63, 185, 80))
        foreach ($pt in $pts) {
            $g.FillEllipse($nodeBrush, $pt.X - $nodeR, $pt.Y - $nodeR, $nodeR * 2, $nodeR * 2)
        }

        # Glowing core
        $glowR = [float]($Size * 0.16)
        $glowBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(70, 63, 185, 80))
        $g.FillEllipse($glowBrush, $cx - $glowR, $cy - $glowR, $glowR * 2, $glowR * 2)

        $coreR = [float]([math]::Max(2.2, $Size * 0.10))
        $coreBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 230, 237, 243))
        $g.FillEllipse($coreBrush, $cx - $coreR, $cy - $coreR, $coreR * 2, $coreR * 2)

        $innerR = [float]([math]::Max(1.2, $Size * 0.055))
        $g.FillEllipse($nodeBrush, $cx - $innerR, $cy - $innerR, $innerR * 2, $innerR * 2)

        $ms = New-Object System.IO.MemoryStream
        try {
            $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
            [byte[]]$pngBytes = $ms.ToArray()
            return ,$pngBytes
        } finally {
            $ms.Dispose()
        }
    } finally {
        $g.Dispose()
        $bmp.Dispose()
    }
}

$sizes = @(256, 48, 32, 16)
$images = @()
foreach ($s in $sizes) {
    [byte[]]$png = New-IconPngBytes -Size $s
    if ($png.Length -lt 64) {
        throw "PNG generation failed for size ${s}px (got $($png.Length) bytes)."
    }
    $images += [PSCustomObject]@{ Size = $s; Bytes = $png }
}

$outDir = Split-Path $OutPath -Parent
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$fs = [System.IO.File]::Open($OutPath, [System.IO.FileMode]::Create)
$bw = New-Object System.IO.BinaryWriter($fs)
try {
    # ICO Header (6 bytes)
    $bw.Write([uint16]0)                # Reserved
    $bw.Write([uint16]1)                # Type = 1 (ICO)
    $bw.Write([uint16]$images.Count)    # Count

    $offset = 6 + (16 * $images.Count)
    foreach ($img in $images) {
        [byte[]]$data = $img.Bytes
        $dim = if ($img.Size -ge 256) { [byte]0 } else { [byte]$img.Size }
        $bw.Write($dim)                 # Width
        $bw.Write($dim)                 # Height
        $bw.Write([byte]0)              # Color count
        $bw.Write([byte]0)              # Reserved
        $bw.Write([uint16]1)            # Planes
        $bw.Write([uint16]32)           # BitCount
        $bw.Write([uint32]$data.Length)
        $bw.Write([uint32]$offset)
        $offset += $data.Length
    }
    foreach ($img in $images) {
        [byte[]]$data = $img.Bytes
        $bw.Write($data, 0, $data.Length)
    }
} finally {
    $bw.Dispose()
    $fs.Dispose()
}

# Validate ICO via .NET System.Drawing.Icon
$testIcon = New-Object System.Drawing.Icon($OutPath)
$testIcon.Dispose()

Write-Host "[OK] Icona generata e validata: $OutPath ($((Get-Item $OutPath).Length) byte; $($sizes -join ', ') px)" -ForegroundColor Green
