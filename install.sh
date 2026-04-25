#!/bin/bash
set -e

echo "Installing serviceman..."

# Create directories
mkdir -p ~/.serviceman/{pids,logs}
mkdir -p ~/bin

# Copy main script
cp serviceman.py ~/.serviceman/

# Create sm wrapper
cat > ~/bin/sm << 'EOF'
#!/bin/bash
python3 ~/.serviceman/serviceman.py "$@"
EOF
chmod +x ~/bin/sm

echo "Done! Make sure ~/bin is in your PATH"
echo "Run 'sm --help' to get started"
