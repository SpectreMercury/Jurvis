#if os(iOS)
import SwiftUI
import AVFoundation

struct ScannerView:UIViewControllerRepresentable {
    var onCode:(String)->Void
    func makeUIViewController(context:Context)->ScannerController{let controller=ScannerController();controller.onCode=onCode;return controller}
    func updateUIViewController(_ uiViewController:ScannerController,context:Context){}
}
final class ScannerController:UIViewController,AVCaptureMetadataOutputObjectsDelegate {
    var onCode:((String)->Void)?
    private let capture=AVCaptureSession()
    private var done=false
    override func viewDidLoad(){
        super.viewDidLoad();view.backgroundColor = .black
        AVCaptureDevice.requestAccess(for:.video){[weak self] allowed in DispatchQueue.main.async{guard let self else{return};if allowed{self.configure()}else{self.showError("请在系统设置中允许 Jurvis 使用相机，或返回粘贴配对链接。")}}}
    }
    func configure(){
        guard let camera=AVCaptureDevice.default(for:.video),let input=try? AVCaptureDeviceInput(device:camera),capture.canAddInput(input)else{showError("相机不可用，请返回使用配对链接。");return}
        capture.addInput(input);let output=AVCaptureMetadataOutput();guard capture.canAddOutput(output)else{return};capture.addOutput(output)
        output.setMetadataObjectsDelegate(self,queue:.main);output.metadataObjectTypes=[.qr]
        let preview=AVCaptureVideoPreviewLayer(session:capture);preview.frame=view.bounds;preview.videoGravity = .resizeAspectFill;view.layer.addSublayer(preview)
        let caption=UILabel();caption.text="扫描 Mac 上的 Jurvis 配对二维码";caption.textColor = .white;caption.textAlignment = .center;caption.translatesAutoresizingMaskIntoConstraints=false;view.addSubview(caption)
        NSLayoutConstraint.activate([caption.centerXAnchor.constraint(equalTo:view.centerXAnchor),caption.bottomAnchor.constraint(equalTo:view.safeAreaLayoutGuide.bottomAnchor,constant:-35)])
        let session=capture;DispatchQueue.global(qos:.userInitiated).async{session.startRunning()}
    }
    func showError(_ text:String){let label=UILabel(frame:view.bounds.insetBy(dx:24,dy:24));label.text=text;label.numberOfLines=0;label.textAlignment = .center;label.textColor = .white;view.addSubview(label)}
    func metadataOutput(_ output:AVCaptureMetadataOutput,didOutput metadataObjects:[AVMetadataObject],from connection:AVCaptureConnection){guard !done,let item=metadataObjects.first as? AVMetadataMachineReadableCodeObject,let value=item.stringValue,value.hasPrefix("jurvis://pair?")else{return};done=true;capture.stopRunning();onCode?(value)}
    override func viewDidDisappear(_ animated:Bool){super.viewDidDisappear(animated);capture.stopRunning()}
}
#endif
