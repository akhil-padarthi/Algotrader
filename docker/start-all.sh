#!/bin/sh
# Runs the trading bot and the dashboard in one container (one shared /data volume).
# The bot restarts itself if it ever exits; the web server is the main process.
set -e

if [ ! -f "${MODEL_DIR:-/data/models}/model.joblib" ]; then
  if [ "${TRAIN_ON_START:-false}" = "true" ]; then
    # for hosts without a persistent disk (data in Supabase): train a fresh model at every start
    echo "No trained model found - training now (TRAIN_ON_START=true), this takes a few minutes..."
    python -m algotrader train || echo "Training failed - the dashboard still starts; check the log above."
  else
    echo "No trained model found in ${MODEL_DIR:-/data/models}."
    echo "Train once with:  docker compose run --rm algotrader python -m algotrader train"
  fi
fi

(
  while true; do
    python -m algotrader bot || true
    echo "bot exited - restarting in 30s"
    sleep 30
  done
) &

exec gunicorn "algotrader.wsgi:app" \
  --bind "0.0.0.0:${PORT:-8000}" \
  --worker-class gthread --workers 1 --threads 16 \
  --timeout 120 --access-logfile - --error-logfile -
