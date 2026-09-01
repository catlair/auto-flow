# 图标（构建前必须生成）

`tauri.conf.json` 的 `bundle.icon` 引用了以下文件，Tauri 编译期 `tauri::generate_context!`
会读取它们，缺失会导致 `npm run tauri dev/build` 失败：

- `32x32.png`
- `128x128.png`
- `128x128@2x.png`
- `icon.icns`
- `icon.ico`

生成方式（需先 `npm install`），用任意一张 1024×1024 的 PNG 源图：

```bash
npm run tauri icon /path/to/source-1024.png
```

该命令会写入上面全部图标文件到本目录。开发期可先用一张占位图。
