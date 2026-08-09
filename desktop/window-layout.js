const TARGET_MIN_WIDTH = 1160;
const TARGET_MIN_HEIGHT = 720;
const TARGET_MAX_WIDTH = 1440;
const TARGET_MAX_HEIGHT = 900;

function getDisplayWindowMetrics(workAreaSize) {
  const screenWidth = Math.max(1, Math.round(workAreaSize.width));
  const screenHeight = Math.max(1, Math.round(workAreaSize.height));
  const minWidth = Math.min(TARGET_MIN_WIDTH, Math.max(320, screenWidth - 16));
  const minHeight = Math.min(TARGET_MIN_HEIGHT, Math.max(300, screenHeight - 40));

  return {
    minWidth,
    minHeight,
    width: Math.max(minWidth, Math.min(Math.round(screenWidth * 0.82), TARGET_MAX_WIDTH)),
    height: Math.max(minHeight, Math.min(Math.round(screenHeight * 0.86), TARGET_MAX_HEIGHT)),
  };
}

function fitBoundsToWorkArea(bounds, workArea) {
  const width = Math.min(bounds.width, workArea.width);
  const height = Math.min(bounds.height, workArea.height);
  const maxX = workArea.x + workArea.width - width;
  const maxY = workArea.y + workArea.height - height;

  return {
    x: Math.max(workArea.x, Math.min(bounds.x, maxX)),
    y: Math.max(workArea.y, Math.min(bounds.y, maxY)),
    width,
    height,
  };
}

module.exports = { fitBoundsToWorkArea, getDisplayWindowMetrics };
