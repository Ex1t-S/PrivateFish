import "dotenv/config";
import express from "express";
import cors from "cors";
import { PrismaClient } from "@prisma/client";

const prisma = new PrismaClient();
const app = express();
const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || "0.0.0.0";
const repoOwner = process.env.UPDATE_REPO_OWNER || "Ex1t-S";
const repoName = process.env.UPDATE_REPO_NAME || "PrivateFish";
const githubLatestReleaseUrl = `https://api.github.com/repos/${repoOwner}/${repoName}/releases/latest`;

app.use(cors());
app.use(express.json({ limit: "5mb" }));
app.use("/downloads", express.static("public/downloads"));

function versionParts(value) {
  const parts = String(value || "").match(/\d+/g);
  return parts ? parts.map((part) => Number(part)) : [0];
}

function compareVersions(left, right) {
  const leftParts = versionParts(left);
  const rightParts = versionParts(right);
  const maxLength = Math.max(leftParts.length, rightParts.length);

  for (let index = 0; index < maxLength; index += 1) {
    const leftValue = leftParts[index] || 0;
    const rightValue = rightParts[index] || 0;
    if (leftValue !== rightValue) {
      return leftValue > rightValue ? 1 : -1;
    }
  }

  return 0;
}

function normalizeAssetName(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function findReleaseExeAsset(release, version) {
  const assets = Array.isArray(release.assets) ? release.assets : [];
  const normalizedVersion = normalizeAssetName(version);
  const prefix = "huanguefishbotv";

  return (
    assets.find((asset) => {
      const name = String(asset.name || "");
      if (!name.toLowerCase().endsWith(".exe")) {
        return false;
      }
      const normalized = normalizeAssetName(name);
      return normalized.startsWith(prefix) && normalized.includes(normalizedVersion);
    }) ||
    assets.find((asset) => {
      const name = String(asset.name || "");
      return name.toLowerCase().endsWith(".exe") && normalizeAssetName(name).startsWith(prefix);
    }) ||
    null
  );
}

async function githubReleaseManifest() {
  const response = await fetch(githubLatestReleaseUrl, {
    headers: {
      Accept: "application/vnd.github+json",
      "User-Agent": "PrivateFish-update-manifest",
      "X-GitHub-Api-Version": "2026-03-10",
    },
  });

  if (!response.ok) {
    throw new Error(`GitHub release request failed with ${response.status}`);
  }

  const release = await response.json();
  const version = String(release.tag_name || release.name || "").replace(/^v/i, "");
  const asset = findReleaseExeAsset(release, version);

  if (!version || !asset) {
    throw new Error("Latest GitHub release has no matching executable asset");
  }

  const browserDownloadUrl = asset.browser_download_url || "";
  if (!browserDownloadUrl) {
    throw new Error("Latest GitHub release executable has no download URL");
  }

  return {
    ok: true,
    source: "github",
    version,
    minVersion: process.env.UPDATE_MIN_VERSION || "",
    downloadUrl: browserDownloadUrl,
    sha256: String(asset.digest || "").replace(/^sha256:/i, ""),
    size: Number(asset.size || 0),
    required: process.env.UPDATE_REQUIRED === "true",
  };
}

function envVersionManifest() {
  const version = process.env.UPDATE_VERSION || "";
  const downloadUrl = process.env.UPDATE_DOWNLOAD_URL || "";
  if (!version || !downloadUrl) {
    return null;
  }

  return {
    ok: true,
    source: "env",
    version,
    minVersion: process.env.UPDATE_MIN_VERSION || "",
    downloadUrl,
    sha256: process.env.UPDATE_SHA256 || "",
    size: Number(process.env.UPDATE_SIZE || 0),
    required: process.env.UPDATE_REQUIRED === "true",
  };
}

app.get("/health", async (_req, res) => {
  try {
    await prisma.$queryRaw`SELECT 1`;
    res.json({ ok: true, db: true });
  } catch (error) {
    res.status(500).json({ ok: false, db: false, error: error.message });
  }
});

app.get("/version", async (req, res) => {
  try {
    const manifest = envVersionManifest() || (await githubReleaseManifest());
    const currentVersion = String(req.query.current || "");

    res.json({
      ...manifest,
      updateAvailable: currentVersion ? compareVersions(manifest.version, currentVersion) > 0 : null,
      updateRequired:
        Boolean(manifest.required) ||
        (currentVersion && manifest.minVersion ? compareVersions(currentVersion, manifest.minVersion) < 0 : false),
    });
  } catch (error) {
    res.status(503).json({ ok: false, error: error.message });
  }
});

process.on("SIGINT", async () => {
  await prisma.$disconnect();
  process.exit(0);
});

app.listen(port, host, () => {
  console.log(`PrivateFish backend listening on http://${host}:${port}`);
});
